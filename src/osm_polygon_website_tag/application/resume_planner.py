"""Deterministic scheduling and status classification for resumable runs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.text_schema import (
    TEXT_DETERMINISTIC_STATUSES,
    TEXT_NULL_STATUS,
    TEXT_TRANSIENT_STATUSES,
    TEXT_UNFINISHED_STATUSES,
)
from osm_polygon_website_tag.runtime.run_state import (
    RunState,
    persist_enrichment_status_summaries,
)

_STATUS_SUMMARY_COLUMNS = {
    "website": "website_text_status",
    "contact_website": "contact_website_text_status",
}


def prioritize_sources(
    sources: list[Path],
    processed_names: Collection[str],
    *,
    retry_names: Collection[str] = (),
    partial_names: Collection[str] = (),
    retry_priorities: Mapping[str, tuple[int, int]] | None = None,
) -> list[Path]:
    """Put the work requiring attention before retries and completed sources.

    ``processed_names`` contains sources whose current work is complete;
    ``retry_names`` contains sources with prior progress that still need a
    retry. ``partial_names`` identifies sources with durable enrichment
    checkpoint parts and therefore gets priority over ordinary retries.
    ``retry_priorities`` contains ``(tier, negative_count)`` values derived
    from status columns: unfinished rows, transient failures, and deterministic
    URL failures are ordered in that sequence. Any source in neither collection
    is genuinely untouched and gets priority. The final path component remains
    the deterministic tie-breaker.
    """
    processed = set(processed_names)
    retries = set(retry_names)
    partial = set(partial_names)
    priorities = retry_priorities or {}

    def key(source: Path) -> tuple[int, tuple[int, int], str]:
        name = source.name
        if name not in processed and name not in retries:
            return (0, (0, 0), source.as_posix())
        if name in partial:
            return (1, priorities.get(name, (0, 0)), source.as_posix())
        if name in retries:
            return (2, priorities.get(name, (99, 0)), source.as_posix())
        return (3, (99, 0), source.as_posix())

    return sorted(sources, key=key)


def summarize_enrichment_status(
    source: Path | pa.Table,
) -> dict[str, dict[str, int]]:
    """Count text statuses using bounded Arrow batches.

    ``source`` may be a table in focused tests or a Parquet path in the
    workflow. Only the two status columns are read; polygon geometry and text
    payloads are never materialised during resume classification.
    """
    counters = {field: Counter() for field in _STATUS_SUMMARY_COLUMNS}
    batches = _status_batches(source)
    if batches is None:
        return {}
    for batch in batches:
        _accumulate_status_batch(counters, batch)
    return {
        field_name: dict(sorted(field_counts.items()))
        for field_name, field_counts in sorted(counters.items())
    }


def _status_batches(source: Path | pa.Table):
    """Return bounded status batches for a table or Parquet shard."""
    if isinstance(source, pa.Table):
        return source.to_batches()
    parquet = pq.ParquetFile(source)
    if any(column not in parquet.schema_arrow.names for column in _STATUS_SUMMARY_COLUMNS.values()):
        return None
    return parquet.iter_batches(
        columns=list(_STATUS_SUMMARY_COLUMNS.values()),
        batch_size=8_192,
    )


def _accumulate_status_batch(counters: dict[str, Counter[str]], batch: pa.RecordBatch) -> None:
    """Accumulate one bounded Arrow status batch."""
    for field_name, column_name in _STATUS_SUMMARY_COLUMNS.items():
        for value in batch.column(column_name).to_pylist():
            counters[field_name][value if isinstance(value, str) else TEXT_NULL_STATUS] += 1


def coerce_enrichment_status_summary(raw: object) -> dict[str, dict[str, int]] | None:
    """Return a valid persisted summary, or ``None`` when it is unusable."""
    if not isinstance(raw, Mapping):
        return None
    summary: dict[str, dict[str, int]] = {}
    for field_name in _STATUS_SUMMARY_COLUMNS:
        field_counts = _coerce_status_field(raw.get(field_name))
        if field_counts is None:
            return None
        summary[field_name] = field_counts
    return summary


def _coerce_status_field(raw: object) -> dict[str, int] | None:
    """Validate and sort one persisted status-count mapping."""
    if not isinstance(raw, Mapping):
        return None
    field_counts: dict[str, int] = {}
    for status, count in raw.items():
        if not _valid_status_count(status, count):
            return None
        field_counts[cast(str, status)] = cast(int, count)
    return dict(sorted(field_counts.items()))


def _valid_status_count(status: object, count: object) -> bool:
    """Return whether one persisted status count has the required shape."""
    return (
        isinstance(status, str)
        and not isinstance(count, bool)
        and isinstance(count, int)
        and count >= 0
    )


def _retry_priority(summary: Mapping[str, Mapping[str, int]]) -> tuple[int, int]:
    """Return a deterministic priority for a shard's remaining text work."""
    unfinished = transient = deterministic = total = 0
    for counts in summary.values():
        for status, count in counts.items():
            total += count
            add_unfinished, add_transient, add_deterministic = _priority_counts(status, count)
            unfinished += add_unfinished
            transient += add_transient
            deterministic += add_deterministic
    return _priority_tier(unfinished, transient, deterministic), -total


def _priority_counts(status: str, count: int) -> tuple[int, int, int]:
    """Return the counter increment for one status category."""
    if status in TEXT_UNFINISHED_STATUSES:
        return count, 0, 0
    if status in TEXT_TRANSIENT_STATUSES or status not in TEXT_DETERMINISTIC_STATUSES:
        return 0, count, 0
    return 0, 0, count


def _priority_tier(unfinished: int, transient: int, deterministic: int) -> int:
    """Rank unfinished, retryable, deterministic, and complete shards."""
    if unfinished:
        return 0
    if transient:
        return 1
    if deterministic:
        return 3
    return 2


def _partial_enrichment_sources(run_dir: Path, sources: Collection[Path]) -> set[str]:
    """Return sources with at least one durable enrichment checkpoint part."""
    names: set[str] = set()
    polygons_dir = run_dir / "polygons"
    for source in sources:
        directory = (
            polygons_dir / f".{source.name.removesuffix('.osm.pbf')}.parquet.enriching.parts"
        )
        if (directory / "checkpoint.json").is_file() and any(directory.glob("part-*.parquet")):
            names.add(source.name)
    return names


def prepare_resume_priorities(
    run_dir: Path,
    state: RunState,
    sources: Collection[Path],
    *,
    retry_names: Collection[str],
) -> tuple[set[str], dict[str, tuple[int, int]]]:
    """Build resume priorities and backfill summaries missing from old runs."""
    retry_name_set = set(retry_names)
    partial_names = _partial_enrichment_sources(run_dir, sources) & retry_name_set
    priorities: dict[str, tuple[int, int]] = {}
    summaries_to_persist: dict[str, dict[str, dict[str, int]]] = {}
    for source in sources:
        name = source.name
        if name not in retry_name_set:
            continue
        summary, should_persist = _source_priority_summary(run_dir, state, source)
        if summary:
            priorities[name] = _retry_priority(summary)
            if should_persist:
                summaries_to_persist[name] = summary
    persist_enrichment_status_summaries(state, summaries_to_persist)
    return partial_names, priorities


def _source_priority_summary(
    run_dir: Path, state: RunState, source: Path
) -> tuple[dict[str, dict[str, int]] | None, bool]:
    """Load a persisted summary or build one from the source shard once."""
    entry = state.sources.get(source.name)
    if entry is None:
        return None, False
    summary = coerce_enrichment_status_summary(entry.get("enrichment_status_counts"))
    if summary is not None:
        return summary, False
    shard = run_dir / "polygons" / f"{source.name.removesuffix('.osm.pbf')}.parquet"
    if not shard.is_file():
        return None, False
    summary = summarize_enrichment_status(shard)
    return (summary, bool(summary))


__all__ = [
    "coerce_enrichment_status_summary",
    "prepare_resume_priorities",
    "prioritize_sources",
    "summarize_enrichment_status",
]
