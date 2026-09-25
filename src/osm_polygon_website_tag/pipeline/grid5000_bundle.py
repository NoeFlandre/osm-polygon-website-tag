"""Shared contract primitives for offline Grid'5000 bundles.

Both staged stages -- language detection and sentence segmentation -- move the
same shapes between the Seagate run and a reserved node: a JSON bundle
manifest, a JSON result receipt, a pinned model identity, and directories that
must be created or replaced without destroying prior work. Those primitives
live here so neither stage owns a private copy of them.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    RunState,
    atomic_write_json,
    transition_status,
)
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_MANIFEST_NAME = "bundle.json"
RESULT_NAME = "result.json"
DEFAULT_GRID_JOB_SECONDS = 1_800
# The Grid'5000 job defaults. scripts/grid5000/_env.sh (grid5000_run_setup)
# repeats the time budget and batch rows as shell literals; an architecture test
# keeps them equal to these constants.
DEFAULT_GRID_TIME_BUDGET_SECONDS = 1_500
DEFAULT_GRID_LANGUAGE_BATCH_ROWS = 256
DEFAULT_GRID_SENTENCE_BATCH_ROWS = 256

_SHA256_LENGTH = 64
_SAFE_FILENAME_SUFFIX = ".parquet"
_HEX_DIGITS = "0123456789abcdef"


def read_object(path: Path, label: str) -> dict[str, object]:
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


def model_payload(model: ModelIdentity) -> dict[str, str]:
    """Serialize a model identity."""
    return {
        "filename": model.filename,
        "repository": model.repository,
        "revision": model.revision,
        "sha256": model.sha256,
    }


def model_from_payload(raw: object) -> ModelIdentity:
    """Parse a model identity object."""
    if not isinstance(raw, dict):
        raise ValueError("model identity must be an object")
    values = {key: value for key, value in raw.items() if isinstance(key, str)}
    return ModelIdentity(
        repository=required_string(values, "repository"),
        filename=required_string(values, "filename"),
        revision=required_string(values, "revision"),
        sha256=sha256_value(values, "sha256"),
    )


def schema_version(payload: Mapping[str, object]) -> int:
    """Validate the bundle/result schema version."""
    value = payload.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int) or value != BUNDLE_SCHEMA_VERSION:
        raise ValueError("unsupported Grid'5000 bundle schema version")
    return value


def required_string(payload: Mapping[str, object], name: str) -> str:
    """Read a required non-empty string field."""
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def sha256_value(payload: Mapping[str, object], name: str) -> str:
    """Read a lowercase SHA-256 hexadecimal field."""
    value = required_string(payload, name)
    if len(value) != _SHA256_LENGTH or any(character not in _HEX_DIGITS for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def positive_int(payload: Mapping[str, object], name: str) -> int:
    """Read a positive integer field without accepting booleans."""
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def nonnegative_int(payload: Mapping[str, object], name: str) -> int:
    """Read a non-negative integer field without accepting booleans."""
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def required_bool(payload: Mapping[str, object], name: str) -> bool:
    """Read a required JSON boolean field."""
    value = payload.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def optional_job_id(value: object) -> str | None:
    """Read an optional non-empty job identifier."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("job_id must be null or a non-empty string")
    return value


def validate_job_id(job_id: str | None) -> None:
    """Reject empty job identifiers before they enter a receipt."""
    if job_id is not None and not job_id:
        raise ValueError("job_id must be null or a non-empty string")


def validate_commit(commit: str) -> None:
    """Require a non-empty source revision without inspecting credentials."""
    if not isinstance(commit, str) or not commit.strip():
        raise ValueError("commit must be a non-empty string")


def safe_filename(value: str) -> str:
    """Allow only a source basename with the expected Parquet suffix."""
    path = Path(value)
    if not value or path.name != value or not value.endswith(_SAFE_FILENAME_SUFFIX):
        raise ValueError(f"unsafe Grid'5000 shard filename: {value!r}")
    return value


def validate_grid_options(time_budget_seconds: float, batch_rows: int) -> None:
    """Keep jobs inside the 30-minute walltime with a cleanup margin."""
    validate_grid_time_budget(time_budget_seconds)
    if isinstance(batch_rows, bool) or not isinstance(batch_rows, int) or batch_rows < 1:
        raise ValueError("batch_rows must be a positive integer")


def validate_grid_time_budget(time_budget_seconds: float) -> None:
    """Validate a finite budget that leaves the fixed job margin."""
    validate_positive_grid_time(time_budget_seconds)
    if time_budget_seconds > DEFAULT_GRID_TIME_BUDGET_SECONDS:
        raise ValueError(f"time_budget_seconds must be in (0, {DEFAULT_GRID_TIME_BUDGET_SECONDS}]")


def validate_positive_grid_time(time_budget_seconds: object) -> None:
    """Validate the positive finite numeric part of a Grid'5000 budget."""
    if isinstance(time_budget_seconds, bool):
        raise ValueError("time_budget_seconds must be positive")
    if not isinstance(time_budget_seconds, (int, float)):
        raise ValueError("time_budget_seconds must be positive")
    if not math.isfinite(float(time_budget_seconds)):
        raise ValueError("time_budget_seconds must be positive")
    if time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive")


def create_bundle_directory(path: Path) -> None:
    """Create a new empty bundle directory without overwriting prior work."""
    if path.exists():
        raise FileExistsError(f"bundle directory already exists: {path}")
    path.mkdir(parents=True)


def stage_model_file(model_path: Path | str, target: Path) -> None:
    """Reuse the immutable model inode when the bundle shares its filesystem."""
    try:
        target.hardlink_to(model_path)
    except OSError:
        shutil.copy2(model_path, target)


def replace_directory(source: Path, target: Path) -> None:
    """Replace a checkpoint directory with rollback on installation failure."""
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.syncing")
    try:
        shutil.copytree(source, temporary)
        backup = backup_directory(target)
    except BaseException:
        remove_directory(temporary)
        raise
    try:
        temporary.replace(target)
    except BaseException:
        restore_directory(target, backup)
        remove_directory(temporary)
        raise

    remove_directory(backup)


def backup_directory(target: Path) -> Path | None:
    """Move an existing directory aside and return its backup path."""
    if not target.exists():
        return None
    if not target.is_dir():
        raise ValueError(f"checkpoint target is not a directory: {target}")
    backup = target.with_name(f".{target.name}.{uuid4().hex}.backup")
    target.replace(backup)
    return backup


def restore_directory(target: Path, backup: Path | None) -> None:
    """Restore a directory backup after a failed replacement."""
    remove_directory(target)
    if backup is not None and backup.exists():
        backup.replace(target)


def remove_directory(path: Path | None) -> None:
    """Remove a temporary directory when it exists."""
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def reject_frozen_snapshot(state: RunState, *, action: str) -> None:
    """Refuse any mutation of a user-frozen snapshot."""
    if (
        state.metadata.get("status") == STATUS_COMPLETE
        and state.metadata.get("snapshot_status") == "done"
    ):
        raise ValueError(f"cannot {action} a frozen snapshot")


def prepare_stage_run_state(state: RunState, *, action: str) -> None:
    """Enter a resumable staged run while preserving frozen snapshots."""
    reject_frozen_snapshot(state, action=action)
    status = state.metadata.get("status")
    if status in {STATUS_EXTRACTED, STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}:
        transition_status(state, STATUS_ENRICHING)
    elif status not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("Grid'5000 preparation requires an extracted/enriched run")


def validate_stage_sync_state(state: RunState, run_id: str, *, action: str) -> None:
    """Ensure a receipt can only mutate its original, unfrozen run."""
    if state.run_id != run_id:
        raise ValueError("bundle run identity does not match target run")
    reject_frozen_snapshot(state, action=action)
    if state.metadata.get("status") not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("Grid'5000 synchronization requires an enriching/enriched run")


def install_validated_shard(
    local: Path,
    remote: Path,
    *,
    validate: Callable[[Path], None],
    checkpoint_directory: Path,
) -> None:
    """Stage, validate, and atomically promote one shard, then drop its checkpoint."""
    staged = local.with_name(f".{local.name}.grid5000-syncing")
    staged.unlink(missing_ok=True)
    try:
        shutil.copy2(remote, staged)
        validate(staged)
        atomic_promote_bundle([(staged, local)])
    finally:
        staged.unlink(missing_ok=True)
    shutil.rmtree(checkpoint_directory, ignore_errors=True)


def receipt_digest(payload: Mapping[str, object]) -> str:
    """Return the short, key-order-independent digest naming a history file."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def is_unfinished_source_shard(
    state: RunState,
    path: Path,
    *,
    label: str,
    needs_work: Callable[[Path], bool],
) -> bool:
    """Validate run membership and return whether one shard still needs a stage."""
    source_name = f"{path.stem}.osm.pbf"
    if source_name not in state.sources:
        raise ValueError(f"{label} shard is not in the source manifest: {path.name}")
    return needs_work(path)


def write_sync_history(
    history_dir: Path,
    stem: str,
    bundle_payload: Mapping[str, object],
    result_payload: Mapping[str, object],
    *,
    completed: bool,
) -> None:
    """Record a receipt-bound synchronization event without source text."""
    history_dir.mkdir(parents=True, exist_ok=True)
    digest = receipt_digest(result_payload)
    atomic_write_json(
        history_dir / f"{stem}-{digest}.json",
        {
            "action": "completed" if completed else "paused",
            "bundle": bundle_payload,
            "result": result_payload,
        },
    )


__all__ = [
    "BUNDLE_MANIFEST_NAME",
    "BUNDLE_SCHEMA_VERSION",
    "DEFAULT_GRID_JOB_SECONDS",
    "DEFAULT_GRID_LANGUAGE_BATCH_ROWS",
    "DEFAULT_GRID_SENTENCE_BATCH_ROWS",
    "DEFAULT_GRID_TIME_BUDGET_SECONDS",
    "RESULT_NAME",
    "backup_directory",
    "create_bundle_directory",
    "install_validated_shard",
    "is_unfinished_source_shard",
    "model_from_payload",
    "model_payload",
    "nonnegative_int",
    "optional_job_id",
    "positive_int",
    "prepare_stage_run_state",
    "read_object",
    "receipt_digest",
    "reject_frozen_snapshot",
    "remove_directory",
    "replace_directory",
    "required_bool",
    "required_string",
    "restore_directory",
    "safe_filename",
    "schema_version",
    "sha256_value",
    "stage_model_file",
    "validate_commit",
    "validate_grid_options",
    "validate_grid_time_budget",
    "validate_job_id",
    "validate_positive_grid_time",
    "validate_stage_sync_state",
    "write_sync_history",
]
