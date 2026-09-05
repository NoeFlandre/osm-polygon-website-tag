"""Bounded, resumable GlotLID detection for one public polygon shard."""

from __future__ import annotations

import math
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.arrow import call_arrow_kernel
from osm_polygon_website_tag.contracts.language_schema import (
    LANGUAGE_COLUMN_NAMES,
    LANGUAGE_SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    schema_matches,
)
from osm_polygon_website_tag.contracts.text_schema import TEXT_STATUSES, TEXT_UNFINISHED_STATUSES
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint, CheckpointStore
from osm_polygon_website_tag.pipeline.glotlid import LanguageDetector, LanguagePrediction
from osm_polygon_website_tag.pipeline.language_detection_checkpoint import (
    language_checkpoint_store,
    load_language_checkpoint,
)
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle

DEFAULT_BATCH_ROWS = 512
_TEXT_COLUMNS = ("website", "contact_website")
_TEXT_STATUS_COLUMNS = ("website_text_status", "contact_website_text_status")


@dataclass(frozen=True)
class LanguageDetectionResult:
    """Outcome of detecting languages for one public polygon shard."""

    shard_path: Path
    row_count: int
    changed: bool
    shard_sha256: str
    max_batch_rows: int
    completed: bool = True
    processed_rows: int = 0


@dataclass(frozen=True)
class _DetectionProgress:
    """Progress produced while processing one bounded shard prefix."""

    processed_rows: int
    max_batch_rows: int
    completed: bool


@dataclass
class _DetectionContext:
    """Validated resources shared by one shard detection invocation."""

    shard: Path
    parquet: pq.ParquetFile
    source_row_count: int
    store: CheckpointStore
    checkpoint: Checkpoint
    staged: Path
    next_part_index: int


def shard_needs_language_detection(shard_path: Path | str) -> bool:
    """Return whether a public shard lacks a complete language result."""
    shard = Path(shard_path)
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    _validate_source_schema(source_schema, shard)
    return _inspect_language_readiness(parquet, shard, validate_statuses=False)


def detect_language_shard(
    shard_path: Path | str,
    *,
    detector: LanguageDetector,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    time_budget_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> LanguageDetectionResult:
    """Detect languages in bounded batches and atomically promote the result."""
    validate_language_detection_options(batch_rows, time_budget_seconds)
    clock_function = clock if clock is not None else monotonic
    deadline = _detection_deadline(time_budget_seconds, clock_function)
    shard = Path(shard_path)
    context = _prepare_detection_context(shard, detector)
    if context is None:
        return _unchanged_detection_result(shard)
    try:
        progress = _process_detection_batches_with_progress(
            context.parquet,
            context.source_row_count,
            context.checkpoint,
            store=context.store,
            next_part_index=context.next_part_index,
            detector=detector,
            batch_rows=batch_rows,
            deadline=deadline,
            clock=clock_function,
        )
        if not progress.completed:
            context.staged.unlink(missing_ok=True)
            return _paused_detection_result(shard, context, progress)
        max_batch_rows = _promote_detected_shard(
            context=context,
            batch_rows=batch_rows,
            max_batch_rows=progress.max_batch_rows,
        )
        shutil.rmtree(context.checkpoint.directory)
    except BaseException:
        context.staged.unlink(missing_ok=True)
        raise
    return _completed_detection_result(shard, context, max_batch_rows)


def validate_language_detection_options(batch_rows: int, time_budget_seconds: float | None) -> None:
    """Validate shared CLI and shard settings before reading run artifacts."""
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")
    if time_budget_seconds is not None:
        _validate_positive_time_value(time_budget_seconds)


def _validate_positive_time_value(time_budget_seconds: object) -> None:
    """Validate one positive finite numeric time budget."""
    if isinstance(time_budget_seconds, bool):
        raise ValueError("time_budget_seconds must be positive")
    if not isinstance(time_budget_seconds, (int, float)):
        raise ValueError("time_budget_seconds must be positive")
    if not math.isfinite(time_budget_seconds):
        raise ValueError("time_budget_seconds must be positive")
    if time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive")


def _detection_deadline(
    time_budget_seconds: float | None,
    clock: Callable[[], float],
) -> float | None:
    """Return a monotonic deadline for a bounded invocation."""
    if time_budget_seconds is None:
        return None
    return clock() + time_budget_seconds


def _unchanged_detection_result(shard: Path) -> LanguageDetectionResult:
    """Build the result for a shard that already has complete languages."""
    row_count = pq.ParquetFile(shard).metadata.num_rows
    return LanguageDetectionResult(
        shard_path=shard,
        row_count=row_count,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=0,
        processed_rows=row_count,
    )


def _paused_detection_result(
    shard: Path,
    context: _DetectionContext,
    progress: _DetectionProgress,
) -> LanguageDetectionResult:
    """Build the result for a budget-exhausted shard."""
    return LanguageDetectionResult(
        shard_path=shard,
        row_count=context.source_row_count,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=progress.max_batch_rows,
        completed=False,
        processed_rows=progress.processed_rows,
    )


def _completed_detection_result(
    shard: Path,
    context: _DetectionContext,
    max_batch_rows: int,
) -> LanguageDetectionResult:
    """Build the result after atomically promoting a completed shard."""
    return LanguageDetectionResult(
        shard_path=shard,
        row_count=context.source_row_count,
        changed=True,
        shard_sha256=hash_shard(shard),
        max_batch_rows=max_batch_rows,
        processed_rows=context.source_row_count,
    )


def _prepare_detection_context(
    shard: Path,
    detector: LanguageDetector,
) -> _DetectionContext | None:
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    _validate_source_schema(source_schema, shard)
    if not _inspect_language_readiness(parquet, shard, validate_statuses=True):
        return None
    source_row_count = parquet.metadata.num_rows
    checkpoint = load_language_checkpoint(
        shard,
        source_row_count=source_row_count,
        source_shard_sha256=hash_shard(shard),
        model=detector.identity,
    )
    staged = shard.with_name(f".{shard.name}.detecting")
    staged.unlink(missing_ok=True)
    return _DetectionContext(
        shard=shard,
        parquet=parquet,
        source_row_count=source_row_count,
        store=language_checkpoint_store(),
        checkpoint=checkpoint,
        staged=staged,
        next_part_index=len(checkpoint.parts),
    )


def _validate_source_schema(source_schema: object, shard: Path) -> None:
    if not (
        schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA)
        or schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA_V1_4)
    ):
        raise ValueError(f"unsupported polygon schema for language detection: {shard.name}")


def _inspect_language_readiness(
    parquet: pq.ParquetFile,
    shard: Path,
    *,
    validate_statuses: bool,
) -> bool:
    """Inspect one shard's readiness in bounded Arrow batches."""
    names = set(parquet.schema_arrow.names)
    missing = set(_TEXT_STATUS_COLUMNS) - names
    if missing:
        raise ValueError(f"missing text status columns for language detection: {sorted(missing)}")
    if schema_matches(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA):
        if validate_statuses:
            _validate_text_statuses(parquet, shard)
        return True
    return _inspect_current_language_readiness(parquet, shard, validate_statuses)


def _inspect_current_language_readiness(
    parquet: pq.ParquetFile,
    shard: Path,
    validate_statuses: bool,
) -> bool:
    """Inspect a v1.4 shard while retaining bounded status validation."""
    columns = [*_TEXT_STATUS_COLUMNS, *LANGUAGE_COLUMN_NAMES]
    needs_detection = False
    for batch in parquet.iter_batches(columns=columns, batch_size=8_192):
        if validate_statuses:
            _validate_status_batch(batch, shard)
        if _batch_needs_language_detection(batch):
            needs_detection = True
            if not validate_statuses:
                return True
    return needs_detection


def _validate_text_statuses(parquet: pq.ParquetFile, shard: Path) -> None:
    names = set(parquet.schema_arrow.names)
    missing = set(_TEXT_STATUS_COLUMNS) - names
    if missing:
        raise ValueError(f"missing text status columns for language detection: {sorted(missing)}")
    for batch in parquet.iter_batches(columns=list(_TEXT_STATUS_COLUMNS), batch_size=8_192):
        _validate_status_batch(batch, shard)


def _validate_status_batch(batch: pa.RecordBatch, shard: Path) -> None:
    """Reject every unknown or unfinished status in one bounded Arrow batch."""
    for column_name in _TEXT_STATUS_COLUMNS:
        _validate_status_values(batch.column(column_name).to_pylist(), shard)


def _validate_status_values(statuses: list[object], shard: Path) -> None:
    """Reject a status sequence containing an unknown or unfinished value."""
    if any(
        status not in TEXT_STATUSES or status in TEXT_UNFINISHED_STATUSES for status in statuses
    ):
        raise ValueError(f"{shard.name} text statuses must be resolved before language detection")


def _batch_needs_language_detection(batch: pa.RecordBatch) -> bool:
    """Return whether either language pair is incomplete in an Arrow batch."""
    return any(
        _language_pair_needs_detection_arrow(batch, prefix)
        for prefix in ("website", "contact_website")
    )


def _language_pair_needs_detection_arrow(batch: pa.RecordBatch, prefix: str) -> bool:
    """Evaluate one language pair without materialising row dictionaries."""
    status = batch.column(f"{prefix}_text_status")
    label = batch.column(f"{prefix}_language")
    probability = batch.column(f"{prefix}_language_probability")
    is_success = call_arrow_kernel("equal", status, "success")
    has_label = call_arrow_kernel(
        "and_kleene",
        call_arrow_kernel("is_valid", label),
        call_arrow_kernel("not_equal", label, ""),
    )
    valid_probability = call_arrow_kernel(
        "and_kleene",
        call_arrow_kernel("is_valid", probability),
        call_arrow_kernel(
            "and_kleene",
            call_arrow_kernel("greater_equal", probability, 0),
            call_arrow_kernel("less_equal", probability, 1),
        ),
    )
    complete = call_arrow_kernel("and_kleene", has_label, valid_probability)
    incomplete_success = call_arrow_kernel(
        "and_kleene", is_success, call_arrow_kernel("invert", complete)
    )
    has_result = call_arrow_kernel(
        "or_kleene",
        call_arrow_kernel("is_valid", label),
        call_arrow_kernel("is_valid", probability),
    )
    non_successful = pc.fill_null(call_arrow_kernel("invert", is_success), True)
    result_on_non_success = call_arrow_kernel("and_kleene", non_successful, has_result)
    needs_detection = call_arrow_kernel("or_kleene", incomplete_success, result_on_non_success)
    return bool(call_arrow_kernel("any", needs_detection).as_py() or False)


def _process_detection_batches_with_progress(
    parquet: pq.ParquetFile,
    source_row_count: int,
    checkpoint: Checkpoint,
    *,
    store: CheckpointStore,
    next_part_index: int,
    detector: LanguageDetector,
    batch_rows: int,
    deadline: float | None,
    clock: Callable[[], float],
) -> _DetectionProgress:
    processed_rows = checkpoint.completed_rows
    rows_to_skip = checkpoint.completed_rows
    max_batch_rows = 0
    for batch in parquet.iter_batches(batch_size=batch_rows):
        originals, rows_to_skip = _skip_checkpointed_rows(batch.to_pylist(), rows_to_skip)
        if not originals:
            continue
        if _deadline_reached(deadline, clock):
            return _DetectionProgress(processed_rows, max_batch_rows, completed=False)
        detected_rows = _detect_batch(originals, detector)
        store.write_part(
            checkpoint.directory,
            next_part_index,
            detected_rows,
            batch_rows=batch_rows,
        )
        next_part_index += 1
        processed_rows += len(detected_rows)
        max_batch_rows = max(max_batch_rows, len(detected_rows))
    if processed_rows != source_row_count:
        raise ValueError("language detection row count changed")
    return _DetectionProgress(processed_rows, max_batch_rows, completed=True)


def _deadline_reached(deadline: float | None, clock: Callable[[], float]) -> bool:
    """Return whether the next detector batch would exceed its budget."""
    return deadline is not None and clock() >= deadline


def _skip_checkpointed_rows(
    originals: list[dict[str, object]],
    rows_to_skip: int,
) -> tuple[list[dict[str, object]], int]:
    if rows_to_skip:
        skipped = min(rows_to_skip, len(originals))
        return originals[skipped:], rows_to_skip - skipped
    return originals, 0


def _detect_batch(
    originals: list[dict[str, object]], detector: LanguageDetector
) -> list[dict[str, object]]:
    rows, pending_by_prefix = _prepare_detection_batch(originals)
    for prefix in ("website", "contact_website"):
        _apply_pending_predictions(prefix, pending_by_prefix[prefix], detector)
    return rows


def _prepare_detection_batch(
    originals: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, list[tuple[dict[str, object], str]]]]:
    """Prepare rows and collect successful texts by independent website field."""
    rows = [_prepare_row(original) for original in originals]
    pending_by_prefix: dict[str, list[tuple[dict[str, object], str]]] = {
        prefix: [] for prefix in _TEXT_COLUMNS
    }
    for row in rows:
        _queue_row_texts(row, pending_by_prefix)
    return rows, pending_by_prefix


def _queue_row_texts(
    row: dict[str, object],
    pending_by_prefix: dict[str, list[tuple[dict[str, object], str]]],
) -> None:
    """Queue successful texts from one row for detection."""
    for prefix in _TEXT_COLUMNS:
        _queue_successful_text(row, prefix, pending_by_prefix[prefix])


def _queue_successful_text(
    row: dict[str, object],
    prefix: str,
    pending: list[tuple[dict[str, object], str]],
) -> None:
    """Validate and queue one successful text field."""
    if row[f"{prefix}_text_status"] != "success":
        return
    text = row.get(f"{prefix}_text")
    if not isinstance(text, str):
        raise ValueError(f"successful {prefix} text is not a string")
    _queue_language_text(row, prefix, text, pending)


def _apply_pending_predictions(
    prefix: str,
    pending: list[tuple[dict[str, object], str]],
    detector: LanguageDetector,
) -> None:
    """Predict and apply one independent website field's pending texts."""
    if not pending:
        return
    predictions = detector.predict([text for _row, text in pending])
    if len(predictions) != len(pending):
        raise ValueError(f"language prediction count does not match {prefix} text count")
    for index, (row, _text) in enumerate(pending):
        _apply_prediction(row, prefix, predictions[index])


def _prepare_row(original: dict[str, object]) -> dict[str, object]:
    row = dict(original)
    for name in LANGUAGE_COLUMN_NAMES:
        if name not in row:
            row[name] = None
    for prefix in _TEXT_COLUMNS:
        if row[f"{prefix}_text_status"] != "success":
            row[f"{prefix}_language"] = None
            row[f"{prefix}_language_probability"] = None
    row["schema_version"] = LANGUAGE_SCHEMA_VERSION
    return row


def _queue_language_text(
    row: dict[str, object],
    prefix: str,
    text: str,
    pending: list[tuple[dict[str, object], str]],
) -> None:
    label_name = f"{prefix}_language"
    probability_name = f"{prefix}_language_probability"
    label = row.get(label_name)
    probability = row.get(probability_name)
    if _empty_language_pair(label, probability):
        pending.append((row, text))
        return
    if _complete_language_pair(label, probability):
        return
    _raise_invalid_language_pair(prefix, label, probability)


def _empty_language_pair(label: object, probability: object) -> bool:
    """Return whether neither language result has been stored yet."""
    return label is None and probability is None


def _raise_invalid_language_pair(prefix: str, label: object, probability: object) -> None:
    """Raise the specific error for an incomplete or invalid existing pair."""
    if label is None or probability is None:
        raise ValueError(f"incomplete {prefix} language pair")
    raise ValueError(f"invalid existing {prefix} language pair")


def _complete_language_pair(label: object, probability: object) -> bool:
    return isinstance(label, str) and bool(label) and _valid_probability(probability)


def _apply_prediction(row: dict[str, object], prefix: str, prediction: LanguagePrediction) -> None:
    if not isinstance(prediction, LanguagePrediction) or not prediction.label:
        raise ValueError(f"invalid {prefix} language prediction")
    if not _valid_probability(prediction.probability):
        raise ValueError(f"invalid {prefix} language probability")
    row[f"{prefix}_language"] = prediction.label
    row[f"{prefix}_language_probability"] = prediction.probability


def _valid_probability(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) <= 1
    )


def _promote_detected_shard(
    *, context: _DetectionContext, batch_rows: int, max_batch_rows: int
) -> int:
    assembled_max_batch_rows = context.store.assemble(
        context.store.parts(context.checkpoint.directory),
        context.staged,
        batch_rows=batch_rows,
        row_count=context.source_row_count,
    )
    max_batch_rows = max(max_batch_rows, assembled_max_batch_rows)
    if not schema_matches(pq.read_schema(context.staged), POLYGON_PUBLIC_SCHEMA_V1_4):
        raise ValueError("detected language shard schema mismatch")
    atomic_promote_bundle([(context.staged, context.shard)])
    return max_batch_rows


__all__ = [
    "DEFAULT_BATCH_ROWS",
    "LanguageDetectionResult",
    "detect_language_shard",
    "shard_needs_language_detection",
    "validate_language_detection_options",
]
