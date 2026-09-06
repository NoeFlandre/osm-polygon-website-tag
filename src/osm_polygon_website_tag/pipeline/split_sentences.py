"""Bounded, resumable sentence segmentation for one public polygon shard.

The stage consumes a v1.4 (language-complete) or v1.5 shard, segments website
text in bounded batches, and atomically promotes a validated v1.5 shard.
Completed batches are source- and model-bound checkpoint parts, so an
interruption or an exhausted time budget resumes without re-segmenting the
durable prefix.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
    schema_matches,
)
from osm_polygon_website_tag.contracts.sentence_schema import SENTENCE_SCHEMA_VERSION
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint, CheckpointStore
from osm_polygon_website_tag.pipeline.sentence_checkpoint import (
    load_sentence_checkpoint,
    sentence_checkpoint_store,
)
from osm_polygon_website_tag.pipeline.sentences import SentenceSplitter, segment_batch
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle

DEFAULT_BATCH_ROWS = 256


@dataclass(frozen=True)
class SentenceSegmentationResult:
    """Outcome of segmenting one polygon shard."""

    shard_path: Path
    row_count: int
    changed: bool
    shard_sha256: str
    max_batch_rows: int
    processed_rows: int
    completed: bool


@dataclass(frozen=True)
class _Progress:
    """Rows processed and batch width observed before stopping.

    A paused run raises :class:`_PausedError` carrying this value instead of
    flagging completion, so there is no boolean to keep in step.
    """

    processed_rows: int
    max_batch_rows: int


class _PausedError(Exception):
    """Signals an exhausted time budget, carrying the progress made so far.

    Pausing is signalled rather than flagged so that no boolean literal
    duplicates what the control flow already says.
    """

    def __init__(self, progress: _Progress) -> None:
        super().__init__()
        self.progress = progress


@dataclass
class _Context:
    """Validated resources shared by one segmentation invocation."""

    shard: Path
    parquet: pq.ParquetFile
    source_row_count: int
    store: CheckpointStore
    checkpoint: Checkpoint
    staged: Path
    next_part_index: int


def shard_needs_sentence_segmentation(shard_path: Path | str) -> bool:
    """Return whether a public shard still lacks its sentence columns."""
    shard = Path(shard_path)
    schema = pq.ParquetFile(shard).schema_arrow
    _validate_source_schema(schema, shard)
    return not schema_matches(schema, POLYGON_PUBLIC_SCHEMA_V1_5)


def segment_sentence_shard(
    shard_path: Path | str,
    *,
    splitter: SentenceSplitter,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    time_budget_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> SentenceSegmentationResult:
    """Segment website text in bounded batches and promote a v1.5 shard."""
    validate_segmentation_options(batch_rows, time_budget_seconds)
    clock_function = clock if clock is not None else monotonic
    deadline = _deadline(time_budget_seconds, clock_function)
    shard = Path(shard_path)
    context = _prepare_context(shard, splitter)
    if context is None:
        return _unchanged_result(shard)
    try:
        progress = _process_batches(
            context,
            splitter=splitter,
            batch_rows=batch_rows,
            deadline=deadline,
            clock=clock_function,
        )
        max_batch_rows = _promote_shard(context, batch_rows, progress.max_batch_rows)
        processed_rows = progress.processed_rows
        shutil.rmtree(context.checkpoint.directory)
    except _PausedError as paused:
        context.staged.unlink(missing_ok=True)
        return _paused_result(shard, context, paused.progress)
    except BaseException:
        context.staged.unlink(missing_ok=True)
        raise
    return _completed_result(shard, context, max_batch_rows, processed_rows)


def validate_segmentation_options(batch_rows: int, time_budget_seconds: float | None) -> None:
    """Validate shared CLI and shard settings before reading run artifacts."""
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")
    if time_budget_seconds is not None and time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive")


def _validate_source_schema(schema: pa.Schema, shard: Path) -> None:
    """Require a language-complete v1.4 shard or an already-segmented v1.5 one."""
    if schema_matches(schema, POLYGON_PUBLIC_SCHEMA_V1_4):
        return
    if schema_matches(schema, POLYGON_PUBLIC_SCHEMA_V1_5):
        return
    raise ValueError(f"unsupported polygon schema for segmentation: {shard.name}")


def _deadline(time_budget_seconds: float | None, clock: Callable[[], float]) -> float | None:
    """Return the monotonic instant at which processing must stop."""
    if time_budget_seconds is None:
        return None
    return clock() + time_budget_seconds


def _deadline_reached(deadline: float | None, clock: Callable[[], float]) -> bool:
    """Return whether the time budget is exhausted."""
    return deadline is not None and clock() >= deadline


def _prepare_context(shard: Path, splitter: SentenceSplitter) -> _Context | None:
    """Validate the shard and open its source/model-bound checkpoint."""
    parquet = pq.ParquetFile(shard)
    _validate_source_schema(parquet.schema_arrow, shard)
    if schema_matches(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_5):
        return None
    source_row_count = parquet.metadata.num_rows
    checkpoint = load_sentence_checkpoint(
        shard,
        source_row_count=source_row_count,
        source_shard_sha256=hash_shard(shard),
        model=splitter.identity,
    )
    staged = shard.with_name(f".{shard.name}.segmenting")
    staged.unlink(missing_ok=True)
    return _Context(
        shard=shard,
        parquet=parquet,
        source_row_count=source_row_count,
        store=sentence_checkpoint_store(),
        checkpoint=checkpoint,
        staged=staged,
        next_part_index=len(checkpoint.parts),
    )


def _process_batches(
    context: _Context,
    *,
    splitter: SentenceSplitter,
    batch_rows: int,
    deadline: float | None,
    clock: Callable[[], float],
) -> _Progress:
    """Segment and checkpoint every unprocessed batch in source order."""
    processed_rows = context.checkpoint.completed_rows
    rows_to_skip = context.checkpoint.completed_rows
    max_batch_rows = 0
    next_part_index = context.next_part_index
    for batch in context.parquet.iter_batches(batch_size=batch_rows):
        originals, rows_to_skip = _skip_checkpointed_rows(batch.to_pylist(), rows_to_skip)
        if not originals:
            continue
        if _deadline_reached(deadline, clock):
            raise _PausedError(_Progress(processed_rows, max_batch_rows))
        segmented = _migrate_rows(segment_batch(originals, splitter))
        context.store.write_part(
            context.checkpoint.directory, next_part_index, segmented, batch_rows=batch_rows
        )
        next_part_index += 1
        processed_rows += len(segmented)
        max_batch_rows = max(max_batch_rows, len(segmented))
    if processed_rows != context.source_row_count:
        raise ValueError("sentence row count changed")
    return _Progress(processed_rows, max_batch_rows)


def _migrate_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Stamp the v1.5 schema version onto every segmented row."""
    for row in rows:
        row["schema_version"] = SENTENCE_SCHEMA_VERSION
    return rows


def _skip_checkpointed_rows(
    originals: list[dict[str, object]], rows_to_skip: int
) -> tuple[list[dict[str, object]], int]:
    """Drop the durable prefix from one Arrow batch."""
    return originals[rows_to_skip:], max(0, rows_to_skip - len(originals))


def _promote_shard(context: _Context, batch_rows: int, max_batch_rows: int) -> int:
    """Assemble checkpoint parts and atomically promote the segmented shard."""
    assembled = context.store.assemble(
        context.store.parts(context.checkpoint.directory),
        context.staged,
        batch_rows=batch_rows,
        row_count=context.source_row_count,
    )
    # The store already validated the assembled schema against v1.5, so there is
    # deliberately no second check here.
    atomic_promote_bundle([(context.staged, context.shard)])
    return max(max_batch_rows, assembled)


def _unchanged_result(shard: Path) -> SentenceSegmentationResult:
    """Report an already-segmented shard without rewriting it."""
    row_count = pq.ParquetFile(shard).metadata.num_rows
    return SentenceSegmentationResult(
        shard_path=shard,
        row_count=row_count,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=0,
        processed_rows=row_count,
        completed=True,
    )


def _paused_result(
    shard: Path, context: _Context, progress: _Progress
) -> SentenceSegmentationResult:
    """Report a run stopped by its time budget, leaving the shard untouched."""
    return SentenceSegmentationResult(
        shard_path=shard,
        row_count=context.source_row_count,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=progress.max_batch_rows,
        processed_rows=progress.processed_rows,
        completed=False,
    )


def _completed_result(
    shard: Path, context: _Context, max_batch_rows: int, processed_rows: int
) -> SentenceSegmentationResult:
    """Report a promoted shard."""
    return SentenceSegmentationResult(
        shard_path=shard,
        row_count=context.source_row_count,
        changed=True,
        shard_sha256=hash_shard(shard),
        max_batch_rows=max_batch_rows,
        processed_rows=processed_rows,
        completed=True,
    )


__all__ = [
    "DEFAULT_BATCH_ROWS",
    "SentenceSegmentationResult",
    "segment_sentence_shard",
    "shard_needs_sentence_segmentation",
    "validate_segmentation_options",
]
