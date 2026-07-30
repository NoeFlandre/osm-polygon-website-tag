"""Tests for the per-shard aggregation logic."""

from __future__ import annotations

import json

import pyarrow as pa

from osm_polygon_website_tag.pipeline.partition_aggregate import (
    aggregate_shard,
)


def _row(
    *,
    polygon_id: str,
    source_pbf: str,
    region: str = "monaco",
    osm_type: str = "way",
    website: str = "https://example.com",
    website_class: str = "absolute_url",
    website_hostname: str | None = "example.com",
    wikidata: str | None = "Q42",
    wikidata_class: str | None = "canonical_qid",
    osm_primary_tag: str = "building",
    area_bucket: str = "10-100m2",
) -> dict[str, object]:
    return {
        "polygon_id": polygon_id,
        "region": region,
        "source_pbf": source_pbf,
        "osm_type": osm_type,
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py(),
        "website": website,
        "website_class": website_class,
        "website_hostname": website_hostname,
        "wikidata": wikidata,
        "wikidata_class": wikidata_class,
        "name": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": osm_primary_tag,
        "geometry": json.dumps({"type": "Polygon", "coordinates": []}),
        "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0.0,0.0,0.0,0.0]",
        "area_m2": 0.0,
        "area_km2": 0.0,
        "area_bucket": area_bucket,
        "extraction_version": "v1.0",
        "extracted_at": pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py(),
    }


def _table(rows: list[dict[str, object]]) -> pa.Table:
    from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA

    return pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA)


def test_aggregate_shard_counts_website_and_wikidata_separately() -> None:
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="monaco-latest.osm.pbf", wikidata="Q42"),
            _row(polygon_id="p2", source_pbf="monaco-latest.osm.pbf", wikidata=None),
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
            _row(
                polygon_id="p1",
                source_pbf="monaco-latest.osm.pbf",
                website="",
                wikidata="Q1",
                wikidata_class="canonical_qid",
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
            _row(polygon_id="p1", source_pbf="x.osm.pbf", website="", wikidata=None),
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
            _row(polygon_id="p1", source_pbf="monaco-latest.osm.pbf"),
            _row(polygon_id="p2", source_pbf="monaco-latest.osm.pbf"),
            _row(polygon_id="p3", source_pbf="rhone-alpes-latest.osm.pbf"),
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
            _row(polygon_id="p1", source_pbf="x.osm.pbf", osm_type="way"),
            _row(polygon_id="p2", source_pbf="x.osm.pbf", osm_type="relation"),
            _row(polygon_id="p3", source_pbf="x.osm.pbf", osm_type="way"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_osm_type_counts == {"way": 2, "relation": 1}


def test_aggregate_shard_per_primary_category_counts() -> None:
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="x.osm.pbf", osm_primary_tag="building"),
            _row(polygon_id="p2", source_pbf="x.osm.pbf", osm_primary_tag="boundary"),
            _row(polygon_id="p3", source_pbf="x.osm.pbf", osm_primary_tag="building"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_primary_category_counts == {"building": 2, "boundary": 1}


def test_aggregate_shard_per_website_class_counts() -> None:
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="x.osm.pbf", website_class="absolute_url"),
            _row(polygon_id="p2", source_pbf="x.osm.pbf", website_class="malformed"),
            _row(polygon_id="p3", source_pbf="x.osm.pbf", website_class="absolute_url"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_website_class_counts == {"absolute_url": 2, "malformed": 1}


def test_aggregate_shard_per_wikidata_class_counts() -> None:
    table = _table(
        [
            _row(
                polygon_id="p1",
                source_pbf="x.osm.pbf",
                region="monaco",
                wikidata="Q1",
                wikidata_class="canonical_qid",
            ),
            _row(
                polygon_id="p2", source_pbf="x.osm.pbf", wikidata="bad", wikidata_class="malformed"
            ),
            _row(
                polygon_id="p3",
                source_pbf="x.osm.pbf",
                wikidata="Q2",
                wikidata_class="canonical_qid",
            ),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_wikidata_class_counts == {"canonical_qid": 2, "malformed": 1}


def test_aggregate_shard_top_hostnames() -> None:
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="x.osm.pbf", website_hostname="example.com"),
            _row(polygon_id="p2", source_pbf="x.osm.pbf", website_hostname="example.com"),
            _row(polygon_id="p3", source_pbf="x.osm.pbf", website_hostname="foo.com"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.top_hostnames == [("example.com", 2), ("foo.com", 1)]


def test_aggregate_shard_per_region_counts() -> None:
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="monaco-latest.osm.pbf", region="monaco"),
            _row(polygon_id="p2", source_pbf="rhone-alpes-latest.osm.pbf", region="rhone-alpes"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_region_counts == {"monaco": 1, "rhone-alpes": 1}


def test_aggregate_shard_per_area_bucket_counts() -> None:
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="x.osm.pbf", area_bucket="10-100m2"),
            _row(polygon_id="p2", source_pbf="x.osm.pbf", area_bucket="100m2-1km2"),
            _row(polygon_id="p3", source_pbf="x.osm.pbf", area_bucket="10-100m2"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.per_area_bucket_counts == {"10-100m2": 2, "100m2-1km2": 1}


def test_aggregate_shard_per_polygon_id_count() -> None:
    """Duplicates inside one shard (rare but possible) should be counted."""
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="x.osm.pbf"),
            _row(polygon_id="p1", source_pbf="x.osm.pbf"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.duplicate_within_shard_count == 2  # two duplicates


def test_aggregate_shard_unique_polygon_ids() -> None:
    table = _table(
        [
            _row(polygon_id="p1", source_pbf="x.osm.pbf"),
            _row(polygon_id="p1", source_pbf="x.osm.pbf"),
            _row(polygon_id="p2", source_pbf="x.osm.pbf"),
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
            _row(polygon_id="p1", source_pbf="x.osm.pbf", wikidata=None),
            _row(polygon_id="p2", source_pbf="x.osm.pbf", wikidata="Q1"),
            _row(polygon_id="p3", source_pbf="x.osm.pbf", wikidata=None),
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
            _row(polygon_id="p1", source_pbf="x.osm.pbf", website_hostname=None),
            _row(polygon_id="p2", source_pbf="x.osm.pbf", website_hostname="example.com"),
        ]
    )
    agg = aggregate_shard(table)
    assert agg.top_hostnames == [("example.com", 1)]
