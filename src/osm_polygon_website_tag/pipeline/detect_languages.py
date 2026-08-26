"""Bounded, resumable GlotLID detection for one public polygon shard."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.language_schema import (
    LANGUAGE_COLUMN_NAMES,
    LANGUAGE_SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    schema_matches,
)
from osm_polygon_website_tag.contracts.text_schema import TEXT_TERMINAL_STATUSES
from osm_polygon_website_tag.pipeline.glotlid import LanguageDetector, LanguagePrediction
from osm_polygon_website_tag.pipeline.language_detection_checkpoint import (
    LanguageCheckpoint,
    assemble_language_checkpoint,
    checkpoint_parts,
    load_language_checkpoint,
    write_language_checkpoint_part,
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


@dataclass
class _DetectionContext:
    """Validated resources shared by one shard detection invocation."""

    shard: Path
    parquet: pq.ParquetFile
    source_row_count: int
    checkpoint: LanguageCheckpoint
    staged: Path
    changed: bool
    next_part_index: int


def shard_needs_language_detection(shard_path: Path | str) -> bool:
    """Return whether a public shard lacks a complete language result."""
    shard = Path(shard_path)
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    _validate_source_schema(source_schema, shard)
    if schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA):
        return True
    columns = [*_TEXT_STATUS_COLUMNS, *LANGUAGE_COLUMN_NAMES]
    for batch in parquet.iter_batches(columns=columns, batch_size=8_192):
        rows = batch.to_pylist()
        if any(_row_needs_language_detection(row) for row in rows):
            return True
    return False


def detect_language_shard(
    shard_path: Path | str,
    *,
    detector: LanguageDetector,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> LanguageDetectionResult:
    """Detect languages in bounded batches and atomically promote the result."""
    _validate_batch_rows(batch_rows)
    shard = Path(shard_path)
    context = _prepare_detection_context(shard, detector, batch_rows)
    if context is None:
        return LanguageDetectionResult(
            shard_path=shard,
            row_count=pq.ParquetFile(shard).metadata.num_rows,
            changed=False,
            shard_sha256=hash_shard(shard),
            max_batch_rows=0,
        )
    try:
        changed_by_batches, max_batch_rows = _process_detection_batches(
            context.parquet,
            context.source_row_count,
            context.checkpoint,
            next_part_index=context.next_part_index,
            detector=detector,
            batch_rows=batch_rows,
        )
        context.changed = context.changed or changed_by_batches
        max_batch_rows = _promote_detected_shard(
            context=context,
            batch_rows=batch_rows,
            max_batch_rows=max_batch_rows,
        )
        shutil.rmtree(context.checkpoint.directory)
    except BaseException:
        context.staged.unlink(missing_ok=True)
        raise
    return LanguageDetectionResult(
        shard_path=shard,
        row_count=context.source_row_count,
        changed=context.changed,
        shard_sha256=hash_shard(shard),
        max_batch_rows=max_batch_rows,
    )


def _validate_batch_rows(batch_rows: int) -> None:
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")


def _prepare_detection_context(
    shard: Path,
    detector: LanguageDetector,
    batch_rows: int,
) -> _DetectionContext | None:
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    _validate_source_schema(source_schema, shard)
    _validate_text_statuses(parquet, shard)
    if not shard_needs_language_detection(shard):
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
        checkpoint=checkpoint,
        staged=staged,
        changed=not schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA_V1_4)
        or bool(checkpoint.parts),
        next_part_index=len(checkpoint.parts),
    )


def _validate_source_schema(source_schema: object, shard: Path) -> None:
    if not (
        schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA)
        or schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA_V1_4)
    ):
        raise ValueError(f"unsupported polygon schema for language detection: {shard.name}")


def _validate_text_statuses(parquet: pq.ParquetFile, shard: Path) -> None:
    names = set(parquet.schema_arrow.names)
    missing = set(_TEXT_STATUS_COLUMNS) - names
    if missing:
        raise ValueError(f"missing text status columns for language detection: {sorted(missing)}")
    for batch in parquet.iter_batches(columns=list(_TEXT_STATUS_COLUMNS), batch_size=8_192):
        _validate_status_batch(batch, shard)


def _validate_status_batch(batch: pa.RecordBatch, shard: Path) -> None:
    """Reject every non-terminal status in one bounded Arrow batch."""
    for column_name in _TEXT_STATUS_COLUMNS:
        _validate_status_values(batch.column(column_name).to_pylist(), shard)


def _validate_status_values(statuses: list[object], shard: Path) -> None:
    """Reject a status sequence containing a non-terminal value."""
    if any(status not in TEXT_TERMINAL_STATUSES for status in statuses):
        raise ValueError(f"{shard.name} text statuses must be terminal before language detection")


def _row_needs_language_detection(row: dict[str, object]) -> bool:
    return any(
        _language_pair_needs_detection(row, prefix) for prefix in ("website", "contact_website")
    )


def _language_pair_needs_detection(row: dict[str, object], prefix: str) -> bool:
    """Return whether one text field lacks a valid language result."""
    status = row[f"{prefix}_text_status"]
    label = row.get(f"{prefix}_language")
    probability = row.get(f"{prefix}_language_probability")
    if status == "success":
        return not _complete_language_pair(label, probability)
    return label is not None or probability is not None


def _process_detection_batches(
    parquet: pq.ParquetFile,
    source_row_count: int,
    checkpoint: LanguageCheckpoint,
    *,
    next_part_index: int,
    detector: LanguageDetector,
    batch_rows: int,
) -> tuple[bool, int]:
    changed = False
    processed_rows = checkpoint.completed_rows
    rows_to_skip = checkpoint.completed_rows
    max_batch_rows = 0
    for batch in parquet.iter_batches(batch_size=batch_rows):
        originals, rows_to_skip = _skip_checkpointed_rows(batch.to_pylist(), rows_to_skip)
        if not originals:
            continue
        detected_rows = _detect_batch(originals, detector)
        write_language_checkpoint_part(
            checkpoint.directory,
            next_part_index,
            detected_rows,
            batch_rows=batch_rows,
        )
        next_part_index += 1
        processed_rows += len(detected_rows)
        max_batch_rows = max(max_batch_rows, len(detected_rows))
        changed = True
    if processed_rows != source_row_count:
        raise ValueError("language detection row count changed")
    return changed, max_batch_rows


def _skip_checkpointed_rows(
    originals: list[dict[str, object]],
    rows_to_skip: int,
) -> tuple[list[dict[str, object]], int]:
    if rows_to_skip >= len(originals):
        return [], rows_to_skip - len(originals)
    if rows_to_skip:
        return originals[rows_to_skip:], 0
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
    for (row, _text), prediction in zip(pending, predictions, strict=True):
        _apply_prediction(row, prefix, prediction)


def _prepare_row(original: dict[str, object]) -> dict[str, object]:
    row = dict(original)
    for name in LANGUAGE_COLUMN_NAMES:
        row.setdefault(name, None)
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
    parts = checkpoint_parts(context.checkpoint.directory)
    assembled_max_batch_rows = assemble_language_checkpoint(
        parts,
        context.staged,
        batch_rows=batch_rows,
        row_count=context.source_row_count,
    )
    max_batch_rows = max(max_batch_rows, assembled_max_batch_rows)
    if not context.changed:
        context.staged.unlink(missing_ok=True)
        return max_batch_rows
    if not schema_matches(pq.read_schema(context.staged), POLYGON_PUBLIC_SCHEMA_V1_4):
        raise ValueError("detected language shard schema mismatch")
    atomic_promote_bundle([(context.staged, context.shard)])
    return max_batch_rows


__all__ = [
    "DEFAULT_BATCH_ROWS",
    "LanguageDetectionResult",
    "detect_language_shard",
    "shard_needs_language_detection",
]
