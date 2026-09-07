"""Offline, resumable Grid'5000 bundles for SaT sentence segmentation.

One bundle carries several small shards rather than a single one: segmentation
is fast enough on short shards that a whole 30-minute reservation would
otherwise be spent staging one country. Shards are packed in deterministic
order up to a row budget, the reserved node segments them under one shared
time budget, and the receipt records every shard the job actually touched --
completed or paused -- so synchronization can install exactly that much.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_5,
    is_current_public_polygon_schema,
    schema_matches,
)
from osm_polygon_website_tag.pipeline.grid5000_bundle import (
    BUNDLE_MANIFEST_NAME,
    BUNDLE_SCHEMA_VERSION,
    DEFAULT_GRID_TIME_BUDGET_SECONDS,
    RESULT_NAME,
    create_bundle_directory,
    model_from_payload,
    model_payload,
    nonnegative_int,
    optional_job_id,
    positive_int,
    read_object,
    replace_directory,
    required_bool,
    required_string,
    safe_filename,
    schema_version,
    sha256_value,
    validate_commit,
    validate_grid_options,
    validate_job_id,
)
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.pipeline.sat import MODEL_REPOSITORY, sat_model_identity
from osm_polygon_website_tag.pipeline.sentence_checkpoint import (
    load_sentence_checkpoint,
    sentence_checkpoint_store,
)
from osm_polygon_website_tag.pipeline.sentence_run import run_sentence_shards
from osm_polygon_website_tag.pipeline.sentences import SentenceSplitter
from osm_polygon_website_tag.pipeline.split_sentences import (
    DEFAULT_BATCH_ROWS,
    SentenceSegmentationResult,
    shard_needs_sentence_segmentation,
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

DEFAULT_GRID_BATCH_ROWS = DEFAULT_BATCH_ROWS
DEFAULT_GRID_MAX_ROWS = 20_000
_POLYGONS_DIRECTORY = "polygons"
_MANIFESTS_DIRECTORY = "manifests"
_HISTORY_DIRECTORY = "grid5000-sentences"


@dataclass(frozen=True)
class SentenceShardEntry:
    """Identity of one shard staged into a sentence bundle."""

    name: str
    row_count: int
    sha256: str

    def payload(self) -> dict[str, object]:
        """Return the stable JSON representation of this shard entry."""
        return {"name": self.name, "row_count": self.row_count, "sha256": self.sha256}


@dataclass(frozen=True)
class SentenceShardOutcome:
    """Segmentation outcome recorded for one shard of a bundle."""

    name: str
    row_count: int
    shard_sha256: str
    completed: bool
    changed: bool
    processed_rows: int
    max_batch_rows: int

    def payload(self) -> dict[str, object]:
        """Return the stable JSON representation of this shard outcome."""
        return {
            "changed": self.changed,
            "completed": self.completed,
            "max_batch_rows": self.max_batch_rows,
            "name": self.name,
            "processed_rows": self.processed_rows,
            "row_count": self.row_count,
            "shard_sha256": self.shard_sha256,
        }


@dataclass(frozen=True)
class SentenceBundle:
    """Source, model, and configuration identity for one staged job."""

    run_id: str
    shards: tuple[SentenceShardEntry, ...]
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
            "model": model_payload(self.model),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "shards": [entry.payload() for entry in self.shards],
            "time_budget_seconds": self.time_budget_seconds,
        }


@dataclass(frozen=True)
class SentenceBundleResult:
    """Validated receipt produced by one offline segmentation job."""

    run_id: str
    shards: tuple[SentenceShardOutcome, ...]
    model: ModelIdentity
    commit: str
    completed: bool
    job_id: str | None
    schema_version: int = BUNDLE_SCHEMA_VERSION

    def payload(self) -> dict[str, object]:
        """Return the stable JSON representation of this result receipt."""
        return {
            "commit": self.commit,
            "completed": self.completed,
            "job_id": self.job_id,
            "model": model_payload(self.model),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "shards": [outcome.payload() for outcome in self.shards],
        }


def prepare_sentence_bundle(
    run_dir: Path | str,
    bundle_dir: Path | str,
    *,
    model_dir: Path | str,
    model_revision: str,
    commit: str,
    time_budget_seconds: int = DEFAULT_GRID_TIME_BUDGET_SECONDS,
    batch_rows: int = DEFAULT_GRID_BATCH_ROWS,
    max_rows: int = DEFAULT_GRID_MAX_ROWS,
) -> SentenceBundle:
    """Stage the next unfinished shards, their checkpoints, and the model."""
    validate_grid_options(time_budget_seconds, batch_rows)
    validate_commit(commit)
    if max_rows < 1:
        raise ValueError("max_rows must be a positive integer")
    state = load_run(Path(run_dir))
    sources = _select_source_shards(state, max_rows=max_rows)
    model = sat_model_identity(model_dir, revision=model_revision)
    bundle = SentenceBundle(
        run_id=state.run_id,
        shards=tuple(_shard_entry(path) for path in sources),
        model=model,
        commit=commit,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
    )
    target = Path(bundle_dir)
    create_bundle_directory(target)
    try:
        _stage_sources(sources, bundle, target)
        _stage_model_directory(Path(model_dir), target / model.filename)
        atomic_write_json(target / BUNDLE_MANIFEST_NAME, bundle.payload())
        _prepare_run_state(state)
    except BaseException:
        shutil.rmtree(target)
        raise
    return bundle


def run_sentence_bundle(
    bundle_dir: Path | str,
    *,
    splitter: SentenceSplitter,
    time_budget_seconds: float | None = None,
    batch_rows: int | None = None,
    job_id: str | None = None,
    clock: Callable[[], float] | None = None,
) -> SentenceBundleResult:
    """Segment every staged shard without downloading or fetching anything."""
    root = Path(bundle_dir)
    bundle = load_sentence_bundle(root)
    result_path = root / RESULT_NAME
    if result_path.exists():
        return _load_result(result_path, bundle)
    if splitter.identity != bundle.model:
        raise ValueError("staged model identity does not match bundle")
    paths = [_validated_staged_shard(root, entry) for entry in bundle.shards]
    budget, rows = _effective_options(bundle, time_budget_seconds, batch_rows)
    outcomes = _segment_staged_shards(
        paths,
        splitter=splitter,
        clock=clock,
        time_budget_seconds=budget,
        batch_rows=rows,
    )
    receipt = _result_from_outcomes(bundle, outcomes, job_id=job_id)
    atomic_write_json(result_path, receipt.payload())
    return receipt


def _effective_options(
    bundle: SentenceBundle,
    time_budget_seconds: float | None,
    batch_rows: int | None,
) -> tuple[float, int]:
    """Resolve and validate the budget and batch width one job will use."""
    budget = bundle.time_budget_seconds if time_budget_seconds is None else time_budget_seconds
    rows = bundle.batch_rows if batch_rows is None else batch_rows
    validate_grid_options(budget, rows)
    return budget, rows


def sync_sentence_bundle(
    bundle_dir: Path | str,
    run_dir: Path | str,
) -> SentenceBundleResult:
    """Install completed shards and paused checkpoints into the canonical run."""
    bundle_root = Path(bundle_dir)
    bundle = load_sentence_bundle(bundle_root)
    result = _load_result(bundle_root / RESULT_NAME, bundle)
    state = load_run(Path(run_dir))
    _validate_sync_state(state, bundle)
    entries = {entry.name: entry for entry in bundle.shards}
    for outcome in result.shards:
        _sync_shard(state, bundle_root, entries[outcome.name], outcome)
    _finish_sync_state(state)
    _write_sync_history(state.run_dir, bundle, result)
    return result


def load_sentence_bundle(bundle_dir: Path | str) -> SentenceBundle:
    """Load and validate a staged bundle manifest."""
    payload = read_object(Path(bundle_dir) / BUNDLE_MANIFEST_NAME, "bundle")
    return _bundle_from_payload(payload)


def _shard_entry(path: Path) -> SentenceShardEntry:
    """Bind one canonical shard to its row count and digest."""
    return SentenceShardEntry(
        name=path.name,
        row_count=pq.ParquetFile(path).metadata.num_rows,
        sha256=hash_shard(path),
    )


def _select_source_shards(state: RunState, *, max_rows: int) -> list[Path]:
    """Pack unfinished shards in stable order up to the row budget."""
    unfinished = [
        path
        for path in sorted((state.run_dir / _POLYGONS_DIRECTORY).glob("*.parquet"))
        if _is_unfinished_source_shard(state, path)
    ]
    if not unfinished:
        raise ValueError("all public shards already carry sentences")
    return _packed_shards(unfinished, max_rows=max_rows)


def _packed_shards(unfinished: list[Path], *, max_rows: int) -> list[Path]:
    """Return the leading shards whose rows fit the budget, at least one."""
    selected: list[Path] = []
    packed_rows = 0
    for path in unfinished:
        rows = pq.ParquetFile(path).metadata.num_rows
        if selected and packed_rows + rows > max_rows:
            break
        selected.append(path)
        packed_rows += rows
    return selected


def _is_unfinished_source_shard(state: RunState, path: Path) -> bool:
    """Validate run membership and return whether one shard needs sentences."""
    source_name = f"{path.stem}.osm.pbf"
    if source_name not in state.sources:
        raise ValueError(f"sentence shard is not in the source manifest: {path.name}")
    return shard_needs_sentence_segmentation(path)


def _stage_sources(sources: Sequence[Path], bundle: SentenceBundle, target: Path) -> None:
    """Copy every selected shard and its existing checkpoint prefix."""
    for path, entry in zip(sources, bundle.shards, strict=True):
        shutil.copy2(path, target / entry.name)
        _copy_checkpoint(path, target, bundle, entry)


def _copy_checkpoint(
    source: Path,
    target: Path,
    bundle: SentenceBundle,
    entry: SentenceShardEntry,
) -> None:
    """Validate and copy an existing source- and model-bound checkpoint."""
    checkpoint_dir = sentence_checkpoint_store().directory_for(source)
    if not checkpoint_dir.exists():
        return
    if not checkpoint_dir.is_dir():
        raise ValueError(f"sentence checkpoint is not a directory: {checkpoint_dir}")
    checkpoint = load_sentence_checkpoint(
        source,
        source_row_count=entry.row_count,
        source_shard_sha256=entry.sha256,
        model=bundle.model,
    )
    shutil.copytree(checkpoint.directory, target / checkpoint.directory.name)


def _stage_model_directory(model_dir: Path, target: Path) -> None:
    """Reuse the immutable model inodes when the bundle shares its filesystem."""
    try:
        shutil.copytree(model_dir, target, copy_function=_hardlink_or_copy)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(model_dir, target)


def _hardlink_or_copy(source: str, destination: str) -> None:
    """Link one staged model file, falling back to a byte copy."""
    try:
        Path(destination).hardlink_to(source)
    except OSError:
        shutil.copy2(source, destination)


def _prepare_run_state(state: RunState) -> None:
    """Enter the resumable sentence stage while preserving frozen snapshots."""
    _reject_frozen_snapshot(state, action="add sentences to")
    status = state.metadata.get("status")
    if status in {STATUS_EXTRACTED, STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}:
        transition_status(state, STATUS_ENRICHING)
    elif status not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("Grid'5000 preparation requires an extracted/enriched run")


def _validate_sync_state(state: RunState, bundle: SentenceBundle) -> None:
    """Ensure a receipt can only mutate its original, unfrozen run."""
    if state.run_id != bundle.run_id:
        raise ValueError("bundle run identity does not match target run")
    _reject_frozen_snapshot(state, action="sync sentences into")
    if state.metadata.get("status") not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("Grid'5000 synchronization requires an enriching/enriched run")


def _reject_frozen_snapshot(state: RunState, *, action: str) -> None:
    """Refuse any mutation of a user-frozen snapshot."""
    if (
        state.metadata.get("status") == STATUS_COMPLETE
        and state.metadata.get("snapshot_status") == "done"
    ):
        raise ValueError(f"cannot {action} a frozen snapshot")


def _validated_staged_shard(root: Path, entry: SentenceShardEntry) -> Path:
    """Validate one staged shard against its bundle entry before loading it."""
    path = root / entry.name
    if not path.is_file():
        raise FileNotFoundError(path)
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows != entry.row_count:
        raise ValueError(f"staged shard row count does not match bundle: {entry.name}")
    if not is_current_public_polygon_schema(parquet.schema_arrow):
        raise ValueError(f"staged shard schema is unsupported: {entry.name}")
    if hash_shard(path) != entry.sha256:
        raise ValueError(f"staged shard hash does not match bundle: {entry.name}")
    return path


def _segment_staged_shards(
    paths: list[Path],
    *,
    splitter: SentenceSplitter,
    batch_rows: int,
    time_budget_seconds: float,
    clock: Callable[[], float] | None,
) -> list[tuple[Path, SentenceSegmentationResult]]:
    """Segment staged shards under one shared budget, keeping every outcome."""
    from osm_polygon_website_tag.pipeline.split_sentences import segment_sentence_shard

    touched: list[tuple[Path, SentenceSegmentationResult]] = []

    def segment(
        shard_path: Path | str,
        *,
        splitter: SentenceSplitter,
        batch_rows: int = DEFAULT_BATCH_ROWS,
        time_budget_seconds: float | None = None,
    ) -> SentenceSegmentationResult:
        result = segment_sentence_shard(
            shard_path,
            splitter=splitter,
            batch_rows=batch_rows,
            time_budget_seconds=time_budget_seconds,
            clock=clock,
        )
        touched.append((Path(shard_path), result))
        return result

    run_sentence_shards(
        paths,
        splitter=splitter,
        record=_ignore_completed_shard,
        batch_rows=batch_rows,
        time_budget_seconds=time_budget_seconds,
        segment=segment,
    )
    return touched


def _ignore_completed_shard(shard: Path, result: SentenceSegmentationResult) -> None:
    """Discard the orchestrator callback: every outcome is captured already."""


def _result_from_outcomes(
    bundle: SentenceBundle,
    touched: list[tuple[Path, SentenceSegmentationResult]],
    *,
    job_id: str | None,
) -> SentenceBundleResult:
    """Project segmentation results into the portable receipt contract."""
    validate_job_id(job_id)
    outcomes = tuple(_outcome(path, result) for path, result in touched)
    completed = len(outcomes) == len(bundle.shards) and all(
        outcome.completed for outcome in outcomes
    )
    return SentenceBundleResult(
        run_id=bundle.run_id,
        shards=outcomes,
        model=bundle.model,
        commit=bundle.commit,
        completed=completed,
        job_id=job_id,
    )


def _outcome(path: Path, result: SentenceSegmentationResult) -> SentenceShardOutcome:
    """Bind one segmentation result to its staged shard name."""
    return SentenceShardOutcome(
        name=path.name,
        row_count=result.row_count,
        shard_sha256=result.shard_sha256,
        completed=result.completed,
        changed=result.changed,
        processed_rows=result.processed_rows,
        max_batch_rows=result.max_batch_rows,
    )


def _sync_shard(
    state: RunState,
    bundle_root: Path,
    entry: SentenceShardEntry,
    outcome: SentenceShardOutcome,
) -> None:
    """Install one completed shard or one paused checkpoint."""
    local = state.run_dir / _POLYGONS_DIRECTORY / entry.name
    remote = bundle_root / entry.name
    if not local.is_file():
        raise FileNotFoundError(local)
    if outcome.completed:
        _sync_completed_shard(state, local, remote, entry, outcome)
    else:
        _sync_paused_checkpoint(local, remote, entry, outcome)


def _sync_completed_shard(
    state: RunState,
    local: Path,
    remote: Path,
    entry: SentenceShardEntry,
    outcome: SentenceShardOutcome,
) -> None:
    """Validate and atomically install one completed v1.5 shard."""
    local_hash = hash_shard(local)
    if local_hash == outcome.shard_sha256:
        _validate_completed_shard(local, outcome)
    else:
        if local_hash != entry.sha256:
            raise ValueError(f"canonical shard changed since bundle preparation: {entry.name}")
        _validate_completed_shard(remote, outcome)
        _install_completed_shard(local, remote, outcome)
    update_public_shard_metadata(
        state,
        filename=f"{local.stem}.osm.pbf",
        row_count=outcome.row_count,
        shard_sha256=outcome.shard_sha256,
    )


def _install_completed_shard(local: Path, remote: Path, outcome: SentenceShardOutcome) -> None:
    """Stage, validate, and atomically promote one segmented shard."""
    staged = local.with_name(f".{local.name}.grid5000-syncing")
    staged.unlink(missing_ok=True)
    try:
        shutil.copy2(remote, staged)
        _validate_completed_shard(staged, outcome)
        atomic_promote_bundle([(staged, local)])
    finally:
        staged.unlink(missing_ok=True)
    shutil.rmtree(sentence_checkpoint_store().directory_for(local), ignore_errors=True)


def _sync_paused_checkpoint(
    local: Path,
    remote: Path,
    entry: SentenceShardEntry,
    outcome: SentenceShardOutcome,
) -> None:
    """Install only a validated checkpoint for a paused shard."""
    if hash_shard(local) != entry.sha256:
        raise ValueError(f"canonical shard changed since bundle preparation: {entry.name}")
    if hash_shard(remote) != entry.sha256:
        raise ValueError(f"paused bundle source changed unexpectedly: {entry.name}")
    checkpoint_dir = sentence_checkpoint_store().directory_for(remote)
    if not checkpoint_dir.is_dir():
        raise ValueError(f"paused shard has no checkpoint: {entry.name}")
    if _checkpoint_rows(checkpoint_dir) != outcome.processed_rows:
        raise ValueError(f"paused result does not match checkpoint progress: {entry.name}")
    replace_directory(checkpoint_dir, sentence_checkpoint_store().directory_for(local))


def _checkpoint_rows(checkpoint_dir: Path) -> int:
    """Count the durable rows a staged checkpoint prefix holds."""
    return sum(
        pq.ParquetFile(part).metadata.num_rows for part in sorted(checkpoint_dir.glob("*.parquet"))
    )


def _validate_completed_shard(path: Path, outcome: SentenceShardOutcome) -> None:
    """Validate schema, row count, digest, and sentence completeness."""
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows != outcome.row_count:
        raise ValueError(f"completed sentence shard row count does not match result: {path.name}")
    if not schema_matches(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_5):
        raise ValueError(f"completed sentence shard schema mismatch: {path.name}")
    if hash_shard(path) != outcome.shard_sha256:
        raise ValueError(f"completed sentence shard hash does not match result: {path.name}")


def _finish_sync_state(state: RunState) -> None:
    """Leave the resumable stage once every shard carries sentences."""
    paths = sorted((state.run_dir / _POLYGONS_DIRECTORY).glob("*.parquet"))
    complete = bool(paths) and all(not shard_needs_sentence_segmentation(path) for path in paths)
    if complete and state.metadata.get("status") == STATUS_ENRICHING:
        transition_status(state, STATUS_ENRICHED)


def _write_sync_history(
    run_dir: Path, bundle: SentenceBundle, result: SentenceBundleResult
) -> None:
    """Record a receipt-bound synchronization event without source text."""
    history_dir = run_dir / _MANIFESTS_DIRECTORY / _HISTORY_DIRECTORY
    history_dir.mkdir(parents=True, exist_ok=True)
    digest = _receipt_digest(result.payload())
    stem = Path(bundle.shards[0].name).stem
    atomic_write_json(
        history_dir / f"{stem}-{digest}.json",
        {
            "action": "completed" if result.completed else "paused",
            "bundle": bundle.payload(),
            "result": result.payload(),
        },
    )


def _receipt_digest(payload: Mapping[str, object]) -> str:
    """Return the short, key-order-independent digest naming a history file."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _load_result(path: Path, bundle: SentenceBundle) -> SentenceBundleResult:
    """Load a receipt and validate that it belongs to this bundle."""
    result = _result_from_payload(read_object(path, "result"))
    _validate_result_binding(result, bundle)
    return result


def _validate_result_binding(result: SentenceBundleResult, bundle: SentenceBundle) -> None:
    """Reject a receipt that does not describe this bundle's staged work."""
    _validate_result_identity(result, bundle)
    staged = _staged_row_counts(bundle)
    for outcome in result.shards:
        _validate_outcome_binding(outcome, staged)


def _validate_result_identity(result: SentenceBundleResult, bundle: SentenceBundle) -> None:
    """Reject a receipt produced for another run, model, or commit."""
    if result.run_id != bundle.run_id:
        raise ValueError("result run identity does not match bundle")
    if result.model != bundle.model:
        raise ValueError("result model identity does not match bundle")
    if result.commit != bundle.commit:
        raise ValueError("result commit does not match bundle")


def _staged_row_counts(bundle: SentenceBundle) -> dict[str, int]:
    """Return the row count staged for each shard name."""
    return {entry.name: entry.row_count for entry in bundle.shards}


def _validate_outcome_binding(outcome: SentenceShardOutcome, staged: dict[str, int]) -> None:
    """Reject one receipt entry that no staged shard accounts for."""
    if outcome.name not in staged:
        raise ValueError(f"result shard is not in the bundle: {outcome.name}")
    if outcome.row_count != staged[outcome.name]:
        raise ValueError(f"result row count does not match bundle: {outcome.name}")


def _bundle_from_payload(payload: Mapping[str, object]) -> SentenceBundle:
    """Parse and validate a bundle manifest object."""
    schema_version(payload)
    model = model_from_payload(payload.get("model"))
    _validate_model(model)
    return SentenceBundle(
        run_id=required_string(payload, "run_id"),
        shards=_shards_from_payload(payload.get("shards")),
        model=model,
        commit=required_string(payload, "commit"),
        time_budget_seconds=positive_int(payload, "time_budget_seconds"),
        batch_rows=positive_int(payload, "batch_rows"),
    )


def _shards_from_payload(raw: object) -> tuple[SentenceShardEntry, ...]:
    """Parse the non-empty staged shard list of a bundle manifest."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("shards must be a non-empty list")
    return tuple(_shard_entry_from_payload(item) for item in raw)


def _shard_entry_from_payload(raw: object) -> SentenceShardEntry:
    """Parse one staged shard entry."""
    if not isinstance(raw, dict):
        raise ValueError("shard entry must be an object")
    values = {key: value for key, value in raw.items() if isinstance(key, str)}
    return SentenceShardEntry(
        name=safe_filename(required_string(values, "name")),
        row_count=nonnegative_int(values, "row_count"),
        sha256=sha256_value(values, "sha256"),
    )


def _result_from_payload(payload: Mapping[str, object]) -> SentenceBundleResult:
    """Parse and validate a result receipt object."""
    schema_version(payload)
    return SentenceBundleResult(
        run_id=required_string(payload, "run_id"),
        shards=_outcomes_from_payload(payload.get("shards")),
        model=model_from_payload(payload.get("model")),
        commit=required_string(payload, "commit"),
        completed=required_bool(payload, "completed"),
        job_id=optional_job_id(payload.get("job_id")),
    )


def _outcomes_from_payload(raw: object) -> tuple[SentenceShardOutcome, ...]:
    """Parse the shard outcome list of a receipt."""
    if not isinstance(raw, list):
        raise ValueError("shards must be a list")
    return tuple(_outcome_from_payload(item) for item in raw)


def _outcome_from_payload(raw: object) -> SentenceShardOutcome:
    """Parse one shard outcome."""
    if not isinstance(raw, dict):
        raise ValueError("shard outcome must be an object")
    values = {key: value for key, value in raw.items() if isinstance(key, str)}
    return SentenceShardOutcome(
        name=safe_filename(required_string(values, "name")),
        row_count=nonnegative_int(values, "row_count"),
        shard_sha256=sha256_value(values, "shard_sha256"),
        completed=required_bool(values, "completed"),
        changed=required_bool(values, "changed"),
        processed_rows=nonnegative_int(values, "processed_rows"),
        max_batch_rows=nonnegative_int(values, "max_batch_rows"),
    )


def _validate_model(model: ModelIdentity) -> None:
    """Require the pinned segmentation repository."""
    if model.repository != MODEL_REPOSITORY:
        raise ValueError("bundle model is not the pinned segmentation model")


__all__ = [
    "DEFAULT_GRID_BATCH_ROWS",
    "DEFAULT_GRID_MAX_ROWS",
    "SentenceBundle",
    "SentenceBundleResult",
    "SentenceShardEntry",
    "SentenceShardOutcome",
    "load_sentence_bundle",
    "prepare_sentence_bundle",
    "run_sentence_bundle",
    "sync_sentence_bundle",
]
