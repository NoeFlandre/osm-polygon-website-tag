"""Offline, resumable Grid'5000 bundles for GlotLID detection."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_4,
    is_current_public_polygon_schema,
    schema_matches,
)
from osm_polygon_website_tag.pipeline.detect_languages import (
    DEFAULT_BATCH_ROWS,
    LanguageDetectionResult,
    detect_language_shard,
    shard_needs_language_detection,
)
from osm_polygon_website_tag.pipeline.glotlid import (
    MODEL_FILENAME,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    ModelIdentity,
    load_glotlid_detector_from_path,
    model_identity_for_path,
)
from osm_polygon_website_tag.pipeline.language_detection_checkpoint import (
    CHECKPOINT_DIRECTORY_SUFFIX,
    load_language_checkpoint,
)
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    RunState,
    atomic_write_json,
    hash_shard,
    load_run,
    transition_status,
    update_public_shard_metadata,
)
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle

BUNDLE_SCHEMA_VERSION = 1
DEFAULT_GRID_JOB_SECONDS = 1_800
DEFAULT_GRID_TIME_BUDGET_SECONDS = 1_500
DEFAULT_GRID_BATCH_ROWS = DEFAULT_BATCH_ROWS
BUNDLE_MANIFEST_NAME = "bundle.json"
RESULT_NAME = "result.json"
_SHA256_LENGTH = 64
_SAFE_FILENAME_SUFFIX = ".parquet"
_POLYGONS_DIRECTORY = "polygons"
_MANIFESTS_DIRECTORY = "manifests"
_GRID5000_DIRECTORY = "grid5000"


@dataclass(frozen=True)
class Grid5000Bundle:
    """Source/model/configuration identity for one staged job."""

    run_id: str
    source_shard: str
    source_row_count: int
    source_shard_sha256: str
    model: ModelIdentity
    commit: str
    time_budget_seconds: int
    batch_rows: int
    schema_version: int = BUNDLE_SCHEMA_VERSION

    def payload(self) -> dict[str, object]:
        """Return the stable JSON representation of this bundle contract."""
        return {
            "batch_rows": self.batch_rows,
            "commit": self.commit,
            "model": _model_payload(self.model),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "source_row_count": self.source_row_count,
            "source_shard": self.source_shard,
            "source_shard_sha256": self.source_shard_sha256,
            "time_budget_seconds": self.time_budget_seconds,
        }


@dataclass(frozen=True)
class Grid5000Result:
    """Validated result receipt produced by one offline job."""

    run_id: str
    source_shard: str
    source_row_count: int
    shard_sha256: str
    model: ModelIdentity
    commit: str
    completed: bool
    changed: bool
    processed_rows: int
    max_batch_rows: int
    job_id: str | None
    schema_version: int = BUNDLE_SCHEMA_VERSION

    def payload(self) -> dict[str, object]:
        """Return the stable JSON representation of this result receipt."""
        return {
            "changed": self.changed,
            "commit": self.commit,
            "completed": self.completed,
            "job_id": self.job_id,
            "max_batch_rows": self.max_batch_rows,
            "model": _model_payload(self.model),
            "processed_rows": self.processed_rows,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "source_row_count": self.source_row_count,
            "source_shard": self.source_shard,
            "shard_sha256": self.shard_sha256,
        }


def prepare_language_bundle(
    run_dir: Path | str,
    bundle_dir: Path | str,
    *,
    model_path: Path | str,
    commit: str,
    time_budget_seconds: int = DEFAULT_GRID_TIME_BUDGET_SECONDS,
    batch_rows: int = DEFAULT_GRID_BATCH_ROWS,
    shard_name: str | None = None,
) -> Grid5000Bundle:
    """Stage one unfinished shard, its checkpoint, and the pinned model."""
    _validate_grid_options(time_budget_seconds, batch_rows)
    _validate_commit(commit)
    root = Path(run_dir)
    target = Path(bundle_dir)
    state = load_run(root)
    source = _select_source_shard(state, shard_name=shard_name)
    source_row_count = pq.ParquetFile(source).metadata.num_rows
    source_hash = hash_shard(source)
    model = model_identity_for_path(model_path)
    bundle = Grid5000Bundle(
        run_id=state.run_id,
        source_shard=source.name,
        source_row_count=source_row_count,
        source_shard_sha256=source_hash,
        model=model,
        commit=commit,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
    )
    _create_bundle_directory(target)
    try:
        shutil.copy2(source, target / bundle.source_shard)
        _stage_model(model_path, target / model.filename)
        _copy_checkpoint(source, target, bundle)
        atomic_write_json(target / BUNDLE_MANIFEST_NAME, bundle.payload())
        _prepare_run_state(state)
    except BaseException:
        shutil.rmtree(target)
        raise
    return bundle


def _stage_model(model_path: Path | str, target: Path) -> None:
    """Reuse the immutable model inode when the bundle shares its filesystem."""
    try:
        target.hardlink_to(model_path)
    except OSError:
        shutil.copy2(model_path, target)


def run_language_bundle(
    bundle_dir: Path | str,
    *,
    time_budget_seconds: float | None = None,
    batch_rows: int | None = None,
    job_id: str | None = None,
    clock: Callable[[], float] | None = None,
) -> Grid5000Result:
    """Run one staged bundle without downloading or fetching anything."""
    root = Path(bundle_dir)
    bundle = _load_bundle(root)
    result_path = root / RESULT_NAME
    source = root / bundle.source_shard
    model_path = root / bundle.model.filename
    if result_path.exists():
        result = _load_result(result_path, bundle)
        _validate_existing_result_artifacts(source, model_path, bundle, result)
        return result
    _validate_bundle_source(source, bundle)
    actual_model = model_identity_for_path(model_path)
    if actual_model != bundle.model:
        raise ValueError("staged model identity does not match bundle")
    effective_budget = (
        bundle.time_budget_seconds if time_budget_seconds is None else time_budget_seconds
    )
    effective_batch_rows = bundle.batch_rows if batch_rows is None else batch_rows
    _validate_grid_options(effective_budget, effective_batch_rows)
    detector = load_glotlid_detector_from_path(model_path)
    detection = detect_language_shard(
        source,
        detector=detector,
        batch_rows=effective_batch_rows,
        time_budget_seconds=effective_budget,
        clock=clock,
    )
    receipt = _result_from_detection(bundle, detection, job_id=job_id)
    atomic_write_json(result_path, receipt.payload())
    return receipt


def _validate_existing_result_artifacts(
    source: Path,
    model_path: Path,
    bundle: Grid5000Bundle,
    result: Grid5000Result,
) -> None:
    """Validate staged artifacts before reusing a prior job receipt."""
    actual_model = model_identity_for_path(model_path)
    if actual_model != bundle.model:
        raise ValueError("staged model identity does not match bundle")
    if result.completed:
        _validate_completed_shard(source, result)
    else:
        _validate_bundle_source(source, bundle)


def sync_language_bundle(
    bundle_dir: Path | str,
    run_dir: Path | str,
) -> Grid5000Result:
    """Install one paused checkpoint or completed shard into the canonical run."""
    bundle_root = Path(bundle_dir)
    bundle = _load_bundle(bundle_root)
    result = _load_result(bundle_root / RESULT_NAME, bundle)
    state = load_run(Path(run_dir))
    _validate_sync_state(state, bundle)
    local = Path(run_dir) / _POLYGONS_DIRECTORY / bundle.source_shard
    remote = bundle_root / bundle.source_shard
    if not local.is_file():
        raise FileNotFoundError(local)
    if result.completed:
        _sync_completed_shard(state, local, remote, bundle, result)
    else:
        _sync_paused_checkpoint(local, remote, bundle, result)
    _write_sync_history(Path(run_dir), bundle, result)
    return result


def _select_source_shard(state: RunState, *, shard_name: str | None) -> Path:
    """Select the first unfinished source shard in stable order."""
    paths = sorted((state.run_dir / _POLYGONS_DIRECTORY).glob("*.parquet"))
    if shard_name is None:
        return _select_first_unfinished_shard(state, paths)
    return _select_named_unfinished_shard(state, paths, shard_name)


def _select_first_unfinished_shard(state: RunState, paths: list[Path]) -> Path:
    """Return the first unfinished shard from a sorted path list."""
    for path in paths:
        if _is_unfinished_source_shard(state, path):
            return path
    raise ValueError("all public shards already contain complete language results")


def _select_named_unfinished_shard(
    state: RunState,
    paths: list[Path],
    shard_name: str,
) -> Path:
    """Return a requested unfinished shard or a precise selection error."""
    name = _safe_filename(shard_name)
    matches = [path for path in paths if path.name == name]
    if not matches:
        raise ValueError(f"selected language shard does not exist: {name}")
    path = matches[0]
    if not _is_unfinished_source_shard(state, path):
        raise ValueError(f"selected language shard is already complete: {path.name}")
    return path


def _is_unfinished_source_shard(state: RunState, path: Path) -> bool:
    """Validate run membership and return whether one shard needs detection."""
    source_name = f"{path.stem}.osm.pbf"
    if source_name not in state.sources:
        raise ValueError(f"language shard is not in the source manifest: {path.name}")
    return shard_needs_language_detection(path)


def _prepare_run_state(state: RunState) -> None:
    """Enter the resumable language stage while preserving frozen snapshots."""
    if (
        state.metadata.get("status") == STATUS_COMPLETE
        and state.metadata.get("snapshot_status") == "done"
    ):
        raise ValueError("cannot add languages to a frozen snapshot")
    status = state.metadata.get("status")
    if status in {STATUS_EXTRACTED, STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}:
        transition_status(state, STATUS_ENRICHING)
    elif status not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("Grid'5000 preparation requires an extracted/enriched run")


def _validate_sync_state(state: RunState, bundle: Grid5000Bundle) -> None:
    """Ensure a receipt can only mutate its original, unfrozen run."""
    if state.run_id != bundle.run_id:
        raise ValueError("bundle run identity does not match target run")
    if (
        state.metadata.get("status") == STATUS_COMPLETE
        and state.metadata.get("snapshot_status") == "done"
    ):
        raise ValueError("cannot sync languages into a frozen snapshot")
    if state.metadata.get("status") not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("Grid'5000 synchronization requires an enriching/enriched run")


def _copy_checkpoint(source: Path, target: Path, bundle: Grid5000Bundle) -> None:
    """Validate and copy an existing source-bound checkpoint prefix."""
    checkpoint_dir = _checkpoint_directory(source)
    if not checkpoint_dir.exists():
        return
    if not checkpoint_dir.is_dir():
        raise ValueError(f"language checkpoint is not a directory: {checkpoint_dir}")
    checkpoint = load_language_checkpoint(
        source,
        source_row_count=bundle.source_row_count,
        source_shard_sha256=bundle.source_shard_sha256,
        model=bundle.model,
    )
    if checkpoint.completed_rows > bundle.source_row_count:
        raise ValueError("language checkpoint exceeds bundle row count")
    shutil.copytree(checkpoint.directory, target / checkpoint.directory.name)


def _sync_completed_shard(
    state: RunState,
    local: Path,
    remote: Path,
    bundle: Grid5000Bundle,
    result: Grid5000Result,
) -> None:
    """Validate and atomically install one completed v1.4 shard."""
    local_hash = hash_shard(local)
    if local_hash == result.shard_sha256:
        _validate_completed_shard(local, result)
    else:
        if local_hash != bundle.source_shard_sha256:
            raise ValueError("canonical shard changed since bundle preparation")
        _validate_completed_shard(remote, result)
        staged = local.with_name(f".{local.name}.grid5000-syncing")
        staged.unlink(missing_ok=True)
        try:
            shutil.copy2(remote, staged)
            _validate_completed_shard(staged, result)
            atomic_promote_bundle([(staged, local)])
        finally:
            staged.unlink(missing_ok=True)
        shutil.rmtree(_checkpoint_directory(local), ignore_errors=True)
    update_public_shard_metadata(
        state,
        filename=f"{local.stem}.osm.pbf",
        row_count=result.source_row_count,
        shard_sha256=result.shard_sha256,
    )
    if (
        _all_language_shards_complete(state.run_dir)
        and state.metadata.get("status") == STATUS_ENRICHING
    ):
        transition_status(state, STATUS_ENRICHED)


def _sync_paused_checkpoint(
    local: Path,
    remote: Path,
    bundle: Grid5000Bundle,
    result: Grid5000Result,
) -> None:
    """Install only a validated checkpoint for a paused job."""
    if hash_shard(local) != bundle.source_shard_sha256:
        raise ValueError("canonical shard changed since bundle preparation")
    if hash_shard(remote) != bundle.source_shard_sha256:
        raise ValueError("paused bundle source changed unexpectedly")
    if not is_current_public_polygon_schema(pq.read_schema(remote)):
        raise ValueError("paused bundle source schema is unsupported")
    checkpoint = load_language_checkpoint(
        remote,
        source_row_count=bundle.source_row_count,
        source_shard_sha256=bundle.source_shard_sha256,
        model=bundle.model,
    )
    if checkpoint.completed_rows != result.processed_rows:
        raise ValueError("paused result does not match checkpoint progress")
    _replace_directory(checkpoint.directory, _checkpoint_directory(local))


def _validate_completed_shard(path: Path, result: Grid5000Result) -> None:
    """Validate schema, row count, digest, and language completeness."""
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows != result.source_row_count:
        raise ValueError("completed language shard row count does not match result")
    if not schema_matches(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_4):
        raise ValueError("completed language shard schema mismatch")
    if hash_shard(path) != result.shard_sha256:
        raise ValueError("completed language shard hash does not match result")
    if shard_needs_language_detection(path):
        raise ValueError("completed language shard still needs detection")


def _all_language_shards_complete(run_dir: Path) -> bool:
    """Return whether every public shard in a run has a complete result."""
    paths = sorted((run_dir / _POLYGONS_DIRECTORY).glob("*.parquet"))
    return bool(paths) and all(not shard_needs_language_detection(path) for path in paths)


def _write_sync_history(run_dir: Path, bundle: Grid5000Bundle, result: Grid5000Result) -> None:
    """Record a receipt-bound synchronization event without source text."""
    history_dir = run_dir / _MANIFESTS_DIRECTORY / _GRID5000_DIRECTORY
    history_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        json.dumps(result.payload(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    path = history_dir / f"{Path(bundle.source_shard).stem}-{digest}.json"
    atomic_write_json(
        path,
        {
            "action": "completed" if result.completed else "paused",
            "bundle": bundle.payload(),
            "result": result.payload(),
        },
    )


def _result_from_detection(
    bundle: Grid5000Bundle,
    detection: LanguageDetectionResult,
    *,
    job_id: str | None,
) -> Grid5000Result:
    """Project the detector result into the portable receipt contract."""
    _validate_job_id(job_id)
    return Grid5000Result(
        run_id=bundle.run_id,
        source_shard=bundle.source_shard,
        source_row_count=detection.row_count,
        shard_sha256=detection.shard_sha256,
        model=bundle.model,
        commit=bundle.commit,
        completed=detection.completed,
        changed=detection.changed,
        processed_rows=detection.processed_rows,
        max_batch_rows=detection.max_batch_rows,
        job_id=job_id,
    )


def _load_bundle(directory: Path) -> Grid5000Bundle:
    """Load and validate a bundle manifest from a staged directory."""
    payload = _read_object(directory / BUNDLE_MANIFEST_NAME, "bundle")
    bundle = _bundle_from_payload(payload)
    _safe_filename(bundle.source_shard)
    if bundle.model.filename != MODEL_FILENAME:
        raise ValueError("bundle model filename is not the pinned GlotLID binary")
    return bundle


def _load_result(path: Path, bundle: Grid5000Bundle) -> Grid5000Result:
    """Load a result receipt and bind it to its bundle identity."""
    result = _result_from_payload(_read_object(path, "result"))
    _validate_result_binding(result, bundle)
    if result.completed is False and result.shard_sha256 != bundle.source_shard_sha256:
        raise ValueError("paused result source hash does not match bundle")
    return result


def _validate_result_binding(result: Grid5000Result, bundle: Grid5000Bundle) -> None:
    """Require receipt identity fields to equal the bundle contract."""
    for actual, expected in (
        (result.run_id, bundle.run_id),
        (result.source_shard, bundle.source_shard),
        (result.model, bundle.model),
        (result.commit, bundle.commit),
        (result.schema_version, bundle.schema_version),
    ):
        if actual != expected:
            raise ValueError("result identity does not match bundle")
    if result.source_row_count != bundle.source_row_count:
        raise ValueError("result row count does not match bundle")


def _bundle_from_payload(payload: Mapping[str, object]) -> Grid5000Bundle:
    """Parse one validated bundle JSON object."""
    bundle = Grid5000Bundle(
        run_id=_required_string(payload, "run_id"),
        source_shard=_safe_filename(_required_string(payload, "source_shard")),
        source_row_count=_nonnegative_int(payload, "source_row_count"),
        source_shard_sha256=_sha256_value(payload, "source_shard_sha256"),
        model=_model_from_payload(payload.get("model")),
        commit=_required_string(payload, "commit"),
        time_budget_seconds=_positive_int(payload, "time_budget_seconds"),
        batch_rows=_positive_int(payload, "batch_rows"),
        schema_version=_schema_version(payload),
    )
    _validate_model(bundle.model)
    _validate_grid_options(bundle.time_budget_seconds, bundle.batch_rows)
    return bundle


def _result_from_payload(payload: Mapping[str, object]) -> Grid5000Result:
    """Parse one validated result JSON object."""
    result = Grid5000Result(
        run_id=_required_string(payload, "run_id"),
        source_shard=_safe_filename(_required_string(payload, "source_shard")),
        source_row_count=_nonnegative_int(payload, "source_row_count"),
        shard_sha256=_sha256_value(payload, "shard_sha256"),
        model=_model_from_payload(payload.get("model")),
        commit=_required_string(payload, "commit"),
        completed=_required_bool(payload, "completed"),
        changed=_required_bool(payload, "changed"),
        processed_rows=_nonnegative_int(payload, "processed_rows"),
        max_batch_rows=_nonnegative_int(payload, "max_batch_rows"),
        job_id=_optional_job_id(payload.get("job_id")),
        schema_version=_schema_version(payload),
    )
    _validate_model(result.model)
    if result.processed_rows > result.source_row_count:
        raise ValueError("result progress exceeds source row count")
    if result.max_batch_rows > result.source_row_count and result.source_row_count > 0:
        raise ValueError("result batch size exceeds source row count")
    return result


def _validate_bundle_source(path: Path, bundle: Grid5000Bundle) -> None:
    """Validate the staged source before loading the model."""
    if not path.is_file():
        raise FileNotFoundError(path)
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows != bundle.source_row_count:
        raise ValueError("staged source row count does not match bundle")
    if not is_current_public_polygon_schema(parquet.schema_arrow):
        raise ValueError("staged source schema is unsupported")
    if hash_shard(path) != bundle.source_shard_sha256:
        raise ValueError("staged source hash does not match bundle")


def _create_bundle_directory(path: Path) -> None:
    """Create a new empty bundle directory without overwriting prior work."""
    if path.exists():
        raise FileExistsError(f"bundle directory already exists: {path}")
    path.mkdir(parents=True)


def _replace_directory(source: Path, target: Path) -> None:
    """Replace a checkpoint directory with rollback on installation failure."""
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.syncing")
    try:
        shutil.copytree(source, temporary)
        backup = _backup_directory(target)
    except BaseException:
        _remove_directory(temporary)
        raise
    try:
        temporary.replace(target)
    except BaseException:
        _restore_directory(target, backup)
        _remove_directory(temporary)
        raise

    _remove_directory(backup)


def _backup_directory(target: Path) -> Path | None:
    """Move an existing directory aside and return its backup path."""
    if not target.exists():
        return None
    if not target.is_dir():
        raise ValueError(f"checkpoint target is not a directory: {target}")
    backup = target.with_name(f".{target.name}.{uuid4().hex}.backup")
    target.replace(backup)
    return backup


def _restore_directory(target: Path, backup: Path | None) -> None:
    """Restore a directory backup after a failed replacement."""
    _remove_directory(target)
    if backup is not None and backup.exists():
        backup.replace(target)


def _remove_directory(path: Path | None) -> None:
    """Remove a temporary directory when it exists."""
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _checkpoint_directory(shard: Path) -> Path:
    """Return the language checkpoint directory for a shard."""
    return shard.with_name(f".{shard.name}{CHECKPOINT_DIRECTORY_SUFFIX}")


def _read_object(path: Path, label: str) -> dict[str, object]:
    """Read a JSON object and normalize malformed receipt errors."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid {label} JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{label} JSON must be an object: {path}")
    return raw


def _model_payload(model: ModelIdentity) -> dict[str, str]:
    """Serialize a model identity."""
    return {
        "filename": model.filename,
        "repository": model.repository,
        "revision": model.revision,
        "sha256": model.sha256,
    }


def _model_from_payload(raw: object) -> ModelIdentity:
    """Parse a model identity object."""
    if not isinstance(raw, dict):
        raise ValueError("model identity must be an object")
    values = {key: value for key, value in raw.items() if isinstance(key, str)}
    return ModelIdentity(
        repository=_required_string(values, "repository"),
        filename=_required_string(values, "filename"),
        revision=_required_string(values, "revision"),
        sha256=_sha256_value(values, "sha256"),
    )


def _validate_model(model: ModelIdentity) -> None:
    """Require the exact pinned GlotLID repository, file, and revision."""
    if (model.repository, model.filename, model.revision) != (
        MODEL_REPOSITORY,
        MODEL_FILENAME,
        MODEL_REVISION,
    ):
        raise ValueError("bundle model is not the pinned GlotLID revision")


def _schema_version(payload: Mapping[str, object]) -> int:
    """Validate the bundle/result schema version."""
    value = payload.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int) or value != BUNDLE_SCHEMA_VERSION:
        raise ValueError("unsupported Grid'5000 bundle schema version")
    return value


def _required_string(payload: Mapping[str, object], name: str) -> str:
    """Read a required non-empty string field."""
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _sha256_value(payload: Mapping[str, object], name: str) -> str:
    """Read a lowercase SHA-256 hexadecimal field."""
    value = _required_string(payload, name)
    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _positive_int(payload: Mapping[str, object], name: str) -> int:
    """Read a positive integer field without accepting booleans."""
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(payload: Mapping[str, object], name: str) -> int:
    """Read a non-negative integer field without accepting booleans."""
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _required_bool(payload: Mapping[str, object], name: str) -> bool:
    """Read a required JSON boolean field."""
    value = payload.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _optional_job_id(value: object) -> str | None:
    """Read an optional non-empty job identifier."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("job_id must be null or a non-empty string")
    return value


def _validate_job_id(job_id: str | None) -> None:
    """Reject empty job identifiers before they enter a receipt."""
    if job_id is not None and not job_id:
        raise ValueError("job_id must be null or a non-empty string")


def _validate_commit(commit: str) -> None:
    """Require a non-empty source revision without inspecting credentials."""
    if not isinstance(commit, str) or not commit.strip():
        raise ValueError("commit must be a non-empty string")


def _safe_filename(value: str) -> str:
    """Allow only a source basename with the expected Parquet suffix."""
    path = Path(value)
    if not value or path.name != value or not value.endswith(_SAFE_FILENAME_SUFFIX):
        raise ValueError(f"unsafe Grid'5000 shard filename: {value!r}")
    return value


def _validate_grid_options(time_budget_seconds: float, batch_rows: int) -> None:
    """Keep jobs inside the 30-minute walltime with a cleanup margin."""
    _validate_grid_time_budget(time_budget_seconds)
    if isinstance(batch_rows, bool) or not isinstance(batch_rows, int) or batch_rows < 1:
        raise ValueError("batch_rows must be a positive integer")


def _validate_grid_time_budget(time_budget_seconds: float) -> None:
    """Validate a finite budget that leaves the fixed job margin."""
    _validate_positive_grid_time(time_budget_seconds)
    if time_budget_seconds > DEFAULT_GRID_TIME_BUDGET_SECONDS:
        raise ValueError(f"time_budget_seconds must be in (0, {DEFAULT_GRID_TIME_BUDGET_SECONDS}]")


def _validate_positive_grid_time(time_budget_seconds: object) -> None:
    """Validate the positive finite numeric part of a Grid'5000 budget."""
    if isinstance(time_budget_seconds, bool):
        raise ValueError("time_budget_seconds must be positive")
    if not isinstance(time_budget_seconds, (int, float)):
        raise ValueError("time_budget_seconds must be positive")
    if not math.isfinite(float(time_budget_seconds)):
        raise ValueError("time_budget_seconds must be positive")
    if time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive")


__all__ = [
    "BUNDLE_MANIFEST_NAME",
    "BUNDLE_SCHEMA_VERSION",
    "DEFAULT_GRID_BATCH_ROWS",
    "DEFAULT_GRID_JOB_SECONDS",
    "DEFAULT_GRID_TIME_BUDGET_SECONDS",
    "RESULT_NAME",
    "Grid5000Bundle",
    "Grid5000Result",
    "prepare_language_bundle",
    "run_language_bundle",
    "sync_language_bundle",
]
