"""Per-shard aggregation: counts exact overlap buckets per shard.

The aggregate is computed by scanning a single shard in a streaming
fashion. Per-shard aggregates are merged by :mod:`osm_polygon_website_tag.pipeline.analyze`
to produce the global analysis tables.

The exact overlap buckets are:

* ``website_count`` -- rows with a non-empty ``website`` tag.
* ``wikidata_count`` -- rows with a non-empty ``wikidata`` tag.
* ``both_count`` -- rows with both.
* ``website_only_count`` -- website but no wikidata.
* ``wikidata_only_count`` -- wikidata but no website.
* ``neither_count`` -- rows where neither tag is present (should not
  occur in practice but is counted for safety).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

import pyarrow as pa

from osm_polygon_website_tag.domain.wikidata import classify_wikidata


@dataclass
class ShardAggregate:
    """Per-shard aggregate statistics."""

    row_count: int = 0
    website_count: int = 0
    wikidata_count: int = 0
    both_count: int = 0
    website_only_count: int = 0
    wikidata_only_count: int = 0
    neither_count: int = 0
    duplicate_within_shard_count: int = 0
    unique_polygon_ids: set[str] = field(default_factory=set)
    per_source_counts: dict[str, int] = field(default_factory=dict)
    per_osm_type_counts: dict[str, int] = field(default_factory=dict)
    per_primary_category_counts: dict[str, int] = field(default_factory=dict)
    per_website_class_counts: dict[str, int] = field(default_factory=dict)
    per_wikidata_class_counts: dict[str, int] = field(default_factory=dict)
    per_region_counts: dict[str, int] = field(default_factory=dict)
    per_area_bucket_counts: dict[str, int] = field(default_factory=dict)
    top_hostnames: list[tuple[str, int]] = field(default_factory=list)


def _nonempty(s: object) -> bool:
    return isinstance(s, str) and bool(s)


def aggregate_shard(table: pa.Table) -> ShardAggregate:
    """Compute a :class:`ShardAggregate` from a single shard table."""
    agg = ShardAggregate()

    website = table["website"].to_pylist()
    tags = table["tags"].to_pylist()
    polygon_ids = table["polygon_id"].to_pylist()
    source_pbf = table["source_pbf"].to_pylist()
    osm_type = table["osm_type"].to_pylist()
    primary = table["osm_primary_tag"].to_pylist()
    website_class = table["website_class"].to_pylist()
    region = table["region"].to_pylist()
    area_bucket = table["area_bucket"].to_pylist()
    hostname = table["website_hostname"].to_pylist()

    ids: list[str] = []
    for i in range(len(polygon_ids)):
        pid = polygon_ids[i]
        ids.append(pid)
        _record_row(
            agg,
            website=website[i],
            tags=tags[i],
            source_pbf=source_pbf[i],
            osm_type=osm_type[i],
            primary=primary[i],
            website_class=website_class[i],
            region=region[i],
            area_bucket=area_bucket[i],
        )

    _finish_identity_counts(agg, ids)
    agg.top_hostnames = _top_hostnames(hostname)

    return agg


def _record_row(
    agg: ShardAggregate,
    *,
    website: object,
    tags: str,
    source_pbf: str,
    osm_type: str,
    primary: str,
    website_class: str,
    region: str,
    area_bucket: str,
) -> None:
    """Accumulate one row's overlap and dimension counts."""
    wikidata = json.loads(tags).get("wikidata")
    has_website = _nonempty(website)
    has_wikidata = _nonempty(wikidata)
    _update_overlap_counts(agg, has_website, has_wikidata)
    for mapping, key in (
        (agg.per_source_counts, source_pbf),
        (agg.per_osm_type_counts, osm_type),
        (agg.per_primary_category_counts, primary),
        (agg.per_website_class_counts, website_class),
        (agg.per_region_counts, region),
        (agg.per_area_bucket_counts, area_bucket),
    ):
        _increment(mapping, key)
    if has_wikidata:
        _increment(agg.per_wikidata_class_counts, classify_wikidata(wikidata).value)


def _update_overlap_counts(agg: ShardAggregate, has_website: bool, has_wikidata: bool) -> None:
    """Update the exact two-tag overlap buckets for one row."""
    agg.website_count += int(has_website)
    agg.wikidata_count += int(has_wikidata)
    bucket = _overlap_bucket(has_website, has_wikidata)
    setattr(agg, bucket, getattr(agg, bucket) + 1)


def _overlap_bucket(has_website: bool, has_wikidata: bool) -> str:
    """Return the exact bucket name for two presence flags."""
    if has_website and has_wikidata:
        return "both_count"
    if has_website:
        return "website_only_count"
    if has_wikidata:
        return "wikidata_only_count"
    return "neither_count"


def _finish_identity_counts(agg: ShardAggregate, ids: list[str]) -> None:
    """Finalize row and duplicate identity counts for one shard."""
    agg.row_count = len(ids)
    agg.unique_polygon_ids = set(ids)
    counts = Counter(ids)
    agg.duplicate_within_shard_count = sum(c for c in counts.values() if c > 1)


def _top_hostnames(hostnames: list[str | None]) -> list[tuple[str, int]]:
    """Return all non-null hostnames in deterministic descending order."""
    host_counter: Counter[str] = Counter(h for h in hostnames if h is not None)
    return sorted(host_counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _increment(mapping: dict[str, int], key: str) -> None:
    """Increment a string-keyed count mapping."""
    mapping[key] = mapping.get(key, 0) + 1


def merge_aggregates(aggs: list[ShardAggregate]) -> ShardAggregate:
    """Merge a list of :class:`ShardAggregate` into one.

    Sets and top-hostnames are recomputed from the union. Counter
    dicts are summed. ``row_count`` is the sum of per-shard row
    counts. ``unique_polygon_ids`` is the union across shards; the
    caller may further inspect duplicates across shards via
    :func:`count_duplicate_ids`.
    """
    out = ShardAggregate()
    if not aggs:
        return out
    host_counter: Counter[str] = Counter()
    for a in aggs:
        _merge_scalar_counts(out, a)
        _merge_dimension_counts(out, a)
        for hostname, count in a.top_hostnames:
            host_counter[hostname] += count
    out.top_hostnames = sorted(host_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return out


def _merge_scalar_counts(out: ShardAggregate, source: ShardAggregate) -> None:
    """Merge scalar counters and identity sets from one shard aggregate."""
    for field_name in (
        "row_count",
        "website_count",
        "wikidata_count",
        "both_count",
        "website_only_count",
        "wikidata_only_count",
        "neither_count",
        "duplicate_within_shard_count",
    ):
        setattr(out, field_name, getattr(out, field_name) + getattr(source, field_name))
    out.unique_polygon_ids |= source.unique_polygon_ids


def _merge_dimension_counts(out: ShardAggregate, source: ShardAggregate) -> None:
    """Sum all per-dimension counter dictionaries."""
    for field_name in (
        "per_source_counts",
        "per_osm_type_counts",
        "per_primary_category_counts",
        "per_website_class_counts",
        "per_wikidata_class_counts",
        "per_region_counts",
        "per_area_bucket_counts",
    ):
        target = getattr(out, field_name)
        for key, value in getattr(source, field_name).items():
            target[key] = target.get(key, 0) + value


def count_duplicate_ids(aggs: list[ShardAggregate]) -> dict[str, int]:
    """Return ``{polygon_id: count}`` for ids that appear in more than one shard.

    Per-shard duplicates are reported in
    ``ShardAggregate.duplicate_within_shard_count``.
    """
    counts: Counter[str] = Counter()
    for a in aggs:
        for pid in a.unique_polygon_ids:
            counts[pid] += 1
    return {pid: c for pid, c in counts.items() if c > 1}


__all__ = [
    "ShardAggregate",
    "aggregate_shard",
    "count_duplicate_ids",
    "merge_aggregates",
]
