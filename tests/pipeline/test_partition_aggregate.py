"""Tests for the per-shard aggregation logic."""

from __future__ import annotations

import pyarrow as pa
import pytest
from tests.fixtures.polygon_shards import partition_aggregate_row

from osm_polygon_website_tag.pipeline.partition_aggregate import (
    ShardAggregate,
    _overlap_bucket,
    aggregate_shard,
    count_duplicate_ids,
    merge_aggregates,
)


@pytest.mark.parametrize(
    ("has_website", "has_wikidata", "expected"),
    [
        (True, True, "both_count"),
        (True, False, "website_only_count"),
        (False, True, "wikidata_only_count"),
        (False, False, "neither_count"),
    ],
)
def test_overlap_bucket_is_exact(has_website: bool, has_wikidata: bool, expected: str) -> None:
    assert _overlap_bucket(has_website, has_wikidata) == expected


def _table(rows: list[dict[str, object]]) -> pa.Table:
    from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA

    return pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA)


def test_aggregate_shard_counts_website_and_wikidata_separately() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="monaco-latest.osm.pbf", wikidata="Q42"
            ),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="monaco-latest.osm.pbf", wikidata=None
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.row_count == 2
    assert agg.website_count == 2
    assert agg.wikidata_count == 1
    assert agg.both_count == 1
    assert agg.website_only_count == 1
    assert agg.wikidata_only_count == 0
    assert agg.neither_count == 0


def test_aggregate_shard_wikidata_only_when_website_empty() -> None:
    """The aggregator handles rows with wikidata but no website even though
    the extractor would not produce such rows. The accounting bucket
    ``wikidata_only_count`` is therefore still defined and tested."""
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1",
                source_pbf="monaco-latest.osm.pbf",
                website="",
                wikidata="Q1",
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.website_count == 0
    assert agg.wikidata_count == 1
    assert agg.both_count == 0
    assert agg.website_only_count == 0
    assert agg.wikidata_only_count == 1


def test_aggregate_shard_neither_when_both_absent() -> None:
    # Empty website should not be allowed by the extractor, but if a
    # test row gets here we count it as "neither" (and ensure
    # website_count only counts non-empty values).
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="x.osm.pbf", website="", wikidata=None
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.website_count == 0
    assert agg.wikidata_count == 0
    assert agg.both_count == 0
    assert agg.website_only_count == 0
    assert agg.wikidata_only_count == 0
    assert agg.neither_count == 1


def test_aggregate_shard_per_source_counts() -> None:
    table = _table(
        [
            partition_aggregate_row(polygon_id="p1", source_pbf="monaco-latest.osm.pbf"),
            partition_aggregate_row(polygon_id="p2", source_pbf="monaco-latest.osm.pbf"),
            partition_aggregate_row(polygon_id="p3", source_pbf="rhone-alpes-latest.osm.pbf"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_source_counts == {
        "monaco-latest.osm.pbf": 2,
        "rhone-alpes-latest.osm.pbf": 1,
    }


def test_aggregate_shard_per_osm_type_counts() -> None:
    table = _table(
        [
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf", osm_type="way"),
            partition_aggregate_row(polygon_id="p2", source_pbf="x.osm.pbf", osm_type="relation"),
            partition_aggregate_row(polygon_id="p3", source_pbf="x.osm.pbf", osm_type="way"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_osm_type_counts == {"way": 2, "relation": 1}


def test_aggregate_shard_per_primary_category_counts() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="x.osm.pbf", osm_primary_tag="building"
            ),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="x.osm.pbf", osm_primary_tag="boundary"
            ),
            partition_aggregate_row(
                polygon_id="p3", source_pbf="x.osm.pbf", osm_primary_tag="building"
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_primary_category_counts == {"building": 2, "boundary": 1}


def test_aggregate_shard_per_website_class_counts() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="x.osm.pbf", website_class="absolute_url"
            ),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="x.osm.pbf", website_class="malformed"
            ),
            partition_aggregate_row(
                polygon_id="p3", source_pbf="x.osm.pbf", website_class="absolute_url"
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_website_class_counts == {"absolute_url": 2, "malformed": 1}


def test_aggregate_shard_per_wikidata_class_counts() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1",
                source_pbf="x.osm.pbf",
                region="monaco",
                wikidata="Q1",
            ),
            partition_aggregate_row(polygon_id="p2", source_pbf="x.osm.pbf", wikidata="bad"),
            partition_aggregate_row(
                polygon_id="p3",
                source_pbf="x.osm.pbf",
                wikidata="Q2",
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_wikidata_class_counts == {"canonical_qid": 2, "malformed": 1}


def test_aggregate_shard_top_hostnames() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="x.osm.pbf", website_hostname="z.example"
            ),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="x.osm.pbf", website_hostname="z.example"
            ),
            partition_aggregate_row(
                polygon_id="p3", source_pbf="x.osm.pbf", website_hostname="a.example"
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.top_hostnames == [("z.example", 2), ("a.example", 1)]


def test_aggregate_shard_sorts_hostname_ties_and_keeps_empty_non_null_names() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="x.osm.pbf", website_hostname="z.example"
            ),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="x.osm.pbf", website_hostname="a.example"
            ),
            partition_aggregate_row(polygon_id="p3", source_pbf="x.osm.pbf", website_hostname=""),
            partition_aggregate_row(polygon_id="p4", source_pbf="x.osm.pbf", website_hostname=None),
        ]
    )

    assert aggregate_shard(table).top_hostnames == [
        ("", 1),
        ("a.example", 1),
        ("z.example", 1),
    ]


def test_aggregate_shard_per_region_counts() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="monaco-latest.osm.pbf", region="monaco"
            ),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="rhone-alpes-latest.osm.pbf", region="rhone-alpes"
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_region_counts == {"monaco": 1, "rhone-alpes": 1}


def test_aggregate_shard_per_area_bucket_counts() -> None:
    table = _table(
        [
            partition_aggregate_row(
                polygon_id="p1", source_pbf="x.osm.pbf", area_bucket="10-100m2"
            ),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="x.osm.pbf", area_bucket="100m2-1km2"
            ),
            partition_aggregate_row(
                polygon_id="p3", source_pbf="x.osm.pbf", area_bucket="10-100m2"
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_area_bucket_counts == {"10-100m2": 2, "100m2-1km2": 1}


def test_aggregate_shard_per_polygon_id_count() -> None:
    """Duplicates inside one shard (rare but possible) should be counted."""
    table = _table(
        [
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf"),
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf"),
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf"),
            partition_aggregate_row(polygon_id="p2", source_pbf="x.osm.pbf"),
            partition_aggregate_row(polygon_id="p2", source_pbf="x.osm.pbf"),
            partition_aggregate_row(polygon_id="p3", source_pbf="x.osm.pbf"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.row_count == 6
    assert agg.unique_polygon_ids == {"p1", "p2", "p3"}
    assert agg.duplicate_within_shard_count == 5


def test_aggregate_shard_unique_polygon_ids() -> None:
    table = _table(
        [
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf"),
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf"),
            partition_aggregate_row(polygon_id="p2", source_pbf="x.osm.pbf"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.unique_polygon_ids == {"p1", "p2"}


def test_aggregate_shard_website_only_count_uses_website_denominator() -> None:
    """The 'website-only' bucket counts rows that have a website and
    no wikidata; the denominator is all rows that have a website, not
    all rows in the shard."""
    table = _table(
        [
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf", wikidata=None),
            partition_aggregate_row(polygon_id="p2", source_pbf="x.osm.pbf", wikidata="Q1"),
            partition_aggregate_row(polygon_id="p3", source_pbf="x.osm.pbf", wikidata=None),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.website_count == 3
    assert agg.wikidata_count == 1
    assert agg.website_only_count == 2
    assert agg.both_count == 1


def test_aggregate_shard_excludes_null_hostname_from_top_hostnames() -> None:
    table = _table(
        [
            partition_aggregate_row(polygon_id="p1", source_pbf="x.osm.pbf", website_hostname=None),
            partition_aggregate_row(
                polygon_id="p2", source_pbf="x.osm.pbf", website_hostname="example.com"
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.top_hostnames == [("example.com", 1)]


def test_merge_aggregates_sorts_hostnames_by_count_then_name() -> None:
    first = ShardAggregate(top_hostnames=[("z.example", 1), ("a.example", 1), ("m.example", 2)])
    second = ShardAggregate(top_hostnames=[("m.example", 1)])

    assert merge_aggregates([first, second]).top_hostnames == [
        ("m.example", 3),
        ("a.example", 1),
        ("z.example", 1),
    ]


def test_count_duplicate_ids_reports_cross_shard_ids_only() -> None:
    first = ShardAggregate(unique_polygon_ids={"p1", "p2"})
    second = ShardAggregate(unique_polygon_ids={"p1", "p3"})

    assert count_duplicate_ids([first, second]) == {"p1": 2}


def test_merge_aggregates_empty_input_returns_zero_aggregate() -> None:
    result = merge_aggregates([])

    assert result.row_count == 0
    assert result.unique_polygon_ids == set()


def test_merge_aggregates_sums_scalars_dimensions_and_hosts() -> None:
    first = ShardAggregate(
        row_count=2,
        website_count=2,
        wikidata_count=1,
        both_count=1,
        website_only_count=1,
        duplicate_within_shard_count=1,
        unique_polygon_ids={"a", "b"},
        per_source_counts={"a.osm.pbf": 2},
        per_osm_type_counts={"way": 2},
        per_primary_category_counts={"building": 2},
        per_website_class_counts={"absolute_url": 2},
        per_wikidata_class_counts={"canonical_qid": 1},
        per_region_counts={"a": 2},
        per_area_bucket_counts={"10-100m2": 2},
        top_hostnames=[("example.com", 2)],
    )
    second = ShardAggregate(
        row_count=1,
        website_count=1,
        wikidata_count=1,
        both_count=1,
        unique_polygon_ids={"b", "c"},
        per_source_counts={"b.osm.pbf": 1},
        per_osm_type_counts={"relation": 1},
        per_primary_category_counts={"boundary": 1},
        per_website_class_counts={"absolute_url": 1},
        per_wikidata_class_counts={"canonical_qid": 1},
        per_region_counts={"b": 1},
        per_area_bucket_counts={"100m2-1km2": 1},
        top_hostnames=[("example.com", 1), ("other.example", 1)],
    )

    result = merge_aggregates([first, second])

    assert result.row_count == 3
    assert result.website_count == 3
    assert result.wikidata_count == 2
    assert result.both_count == 2
    assert result.duplicate_within_shard_count == 1
    assert result.unique_polygon_ids == {"a", "b", "c"}
    assert result.per_source_counts == {"a.osm.pbf": 2, "b.osm.pbf": 1}
    assert result.per_osm_type_counts == {"way": 2, "relation": 1}
    assert result.per_primary_category_counts == {"building": 2, "boundary": 1}
    assert result.per_website_class_counts == {"absolute_url": 3}
    assert result.per_wikidata_class_counts == {"canonical_qid": 2}
    assert result.per_region_counts == {"a": 2, "b": 1}
    assert result.per_area_bucket_counts == {"10-100m2": 2, "100m2-1km2": 1}
    assert result.top_hostnames == [("example.com", 3), ("other.example", 1)]
