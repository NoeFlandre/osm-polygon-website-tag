"""Deterministic scheduling and status classification for resumable runs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.runtime.run_state import (
    RunState,
    persist_enrichment_status_summaries,
)

_STATUS_SUMMARY_COLUMNS = {
    "website": "website_text_status",
    "contact_website": "contact_website_text_status",
}
_NULL_STATUS = "__null__"
_UNFINISHED_STATUSES = frozenset({"pending", _NULL_STATUS})
_TRANSIENT_STATUSES = frozenset({"empty", "fetch_error", "extract_error"})
_DETERMINISTIC_STATUSES = frozenset({"invalid_url", "unsafe_url"})


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
    if isinstance(source, pa.Table):
        batches = source.to_batches()
    else:
        parquet = pq.ParquetFile(source)
        if any(
            column not in parquet.schema_arrow.names for column in _STATUS_SUMMARY_COLUMNS.values()
        ):
            return {}
        batches = parquet.iter_batches(
            columns=list(_STATUS_SUMMARY_COLUMNS.values()),
            batch_size=8_192,
        )
    for batch in batches:
        for field_name, column_name in _STATUS_SUMMARY_COLUMNS.items():
            for value in batch.column(column_name).to_pylist():
                counters[field_name][value if isinstance(value, str) else _NULL_STATUS] += 1
    return {
        field_name: dict(sorted(field_counts.items()))
        for field_name, field_counts in sorted(counters.items())
    }


def coerce_enrichment_status_summary(raw: object) -> dict[str, dict[str, int]] | None:
    """Return a valid persisted summary, or ``None`` when it is unusable."""
    if not isinstance(raw, Mapping):
        return None
    summary: dict[str, dict[str, int]] = {}
    for field_name in _STATUS_SUMMARY_COLUMNS:
        counts = raw.get(field_name)
        if not isinstance(counts, Mapping):
            return None
        field_counts: dict[str, int] = {}
        for status, count in counts.items():
            if (
                not isinstance(status, str)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                return None
            field_counts[status] = count
        summary[field_name] = dict(sorted(field_counts.items()))
    return summary


def _retry_priority(summary: Mapping[str, Mapping[str, int]]) -> tuple[int, int]:
    """Return a deterministic priority for a shard's remaining text work."""
    unfinished = transient = deterministic = total = 0
    for counts in summary.values():
        for status, count in counts.items():
            total += count
            if status in _UNFINISHED_STATUSES:
                unfinished += count
            elif status in _TRANSIENT_STATUSES:
                transient += count
            elif status in _DETERMINISTIC_STATUSES:
                deterministic += count
            else:
                # Unknown non-terminal values remain actionable and are safer
                # to process before deterministic URL rejections.
                transient += count
    if unfinished:
        tier = 0
    elif transient:
        tier = 1
    elif deterministic:
        tier = 3
    else:
        tier = 2
    return tier, -total


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
        entry = state.sources.get(name)
        if entry is None:
            continue
        summary = coerce_enrichment_status_summary(entry.get("enrichment_status_counts"))
        if summary is None:
            shard = run_dir / "polygons" / f"{source.name.removesuffix('.osm.pbf')}.parquet"
            if shard.is_file():
                summary = summarize_enrichment_status(shard)
                if summary:
                    summaries_to_persist[name] = summary
        if summary:
            priorities[name] = _retry_priority(summary)
    persist_enrichment_status_summaries(state, summaries_to_persist)
    return partial_names, priorities


__all__ = [
    "coerce_enrichment_status_summary",
    "prepare_resume_priorities",
    "prioritize_sources",
    "summarize_enrichment_status",
]
