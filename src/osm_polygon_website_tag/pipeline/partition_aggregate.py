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
        ws = website[i]
        raw_tags = json.loads(tags[i])
        wd = raw_tags.get("wikidata")
        has_ws = _nonempty(ws)
        has_wd = _nonempty(wd)
        if has_ws:
            agg.website_count += 1
        if has_wd:
            agg.wikidata_count += 1
        if has_ws and has_wd:
            agg.both_count += 1
        elif has_ws:
            agg.website_only_count += 1
        elif has_wd:
            agg.wikidata_only_count += 1
        else:
            agg.neither_count += 1
        agg.per_source_counts[source_pbf[i]] = agg.per_source_counts.get(source_pbf[i], 0) + 1
        agg.per_osm_type_counts[osm_type[i]] = agg.per_osm_type_counts.get(osm_type[i], 0) + 1
        agg.per_primary_category_counts[primary[i]] = (
            agg.per_primary_category_counts.get(primary[i], 0) + 1
        )
        agg.per_website_class_counts[website_class[i]] = (
            agg.per_website_class_counts.get(website_class[i], 0) + 1
        )
        if has_wd:
            wikidata_class = classify_wikidata(wd).value
            agg.per_wikidata_class_counts[wikidata_class] = (
                agg.per_wikidata_class_counts.get(wikidata_class, 0) + 1
            )
        agg.per_region_counts[region[i]] = agg.per_region_counts.get(region[i], 0) + 1
        agg.per_area_bucket_counts[area_bucket[i]] = (
            agg.per_area_bucket_counts.get(area_bucket[i], 0) + 1
        )

    agg.row_count = len(polygon_ids)
    agg.unique_polygon_ids = set(ids)
    counts = Counter(ids)
    agg.duplicate_within_shard_count = sum(c for c in counts.values() if c > 1)

    host_counter: Counter[str] = Counter()
    for h in hostname:
        if h is None:
            continue
        host_counter[h] += 1
    agg.top_hostnames = sorted(host_counter.items(), key=lambda kv: (-kv[1], kv[0]))

    return agg


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
    counter_keys = [
        "per_source_counts",
        "per_osm_type_counts",
        "per_primary_category_counts",
        "per_website_class_counts",
        "per_wikidata_class_counts",
        "per_region_counts",
        "per_area_bucket_counts",
    ]
    host_counter: Counter[str] = Counter()
    for a in aggs:
        out.row_count += a.row_count
        out.website_count += a.website_count
        out.wikidata_count += a.wikidata_count
        out.both_count += a.both_count
        out.website_only_count += a.website_only_count
        out.wikidata_only_count += a.wikidata_only_count
        out.neither_count += a.neither_count
        out.duplicate_within_shard_count += a.duplicate_within_shard_count
        out.unique_polygon_ids |= a.unique_polygon_ids
        for k in counter_keys:
            d = getattr(out, k)
            for key, v in getattr(a, k).items():
                d[key] = d.get(key, 0) + v
        for h, n in a.top_hostnames:
            host_counter[h] += n
    out.top_hostnames = sorted(host_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return out


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
