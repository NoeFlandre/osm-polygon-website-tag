"""Bounded, atomic migration of public polygon shards."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_2,
)
from osm_polygon_website_tag.pipeline.public_schema_migration import (
    _validate_migrated_shard,
    _write_migration_batch,
    migrate_public_shard,
)
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink


def _v1_2_row() -> dict[str, object]:
    return {
        "polygon_id": "source:way/1",
        "region": "source",
        "source_pbf": "source.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_version": 1,
        "osm_timestamp": pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py(),
        "name": "Example",
        "website": "https://example.org",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.org",
        "contact_website_hostname": None,
        "preferred_website": "https://example.org",
        "preferred_website_source": "website",
        "wikidata": "Q42",
        "wikidata_qid": "Q42",
        "wikidata_class": "canonical_qid",
        "tags": json.dumps({"website": "https://example.org", "wikidata": "Q42"}),
        "tag_keys": '["website","wikidata"]',
        "tag_count": 2,
        "osm_primary_tag": "building",
        "geometry": '{"type":"Polygon","coordinates":[]}',
        "centroid": '{"type":"Point","coordinates":[0,0]}',
        "centroid_kind": "lambert_azimuthal_equal_area",
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0,0,0,0]",
        "area_m2": 1.0,
        "area_km2": 0.000001,
        "area_bucket": "<10m2",
        "schema_version": "v1.2",
        "website_text": "full website text",
        "website_word_count": 3,
        "website_text_status": "success",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _write_v1_2(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA_V1_2), path)


def test_migrate_v1_2_projects_only_removed_columns_and_preserves_text(tmp_path: Path) -> None:
    shard = tmp_path / "source.parquet"
    _write_v1_2(shard, [_v1_2_row()])

    result = migrate_public_shard(shard, batch_rows=1)

    table = pq.read_table(shard)
    assert table.schema.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert table.column_names == POLYGON_PUBLIC_SCHEMA.names
    row = table.to_pylist()[0]
    assert row["polygon_id"] == "source:way/1"
    assert row["website_text"] == "full website text"
    assert row["website_word_count"] == 3
    assert row["schema_version"] == "v1.3"
    assert result.changed is True
    assert result.row_count == 1
    assert result.max_batch_rows == 1
    assert result.shard_sha256 == hash_shard(shard)


def test_migrate_empty_v1_2_shard(tmp_path: Path) -> None:
    shard = tmp_path / "empty.parquet"
    _write_v1_2(shard, [])

    result = migrate_public_shard(shard)

    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert result.changed is True
    assert result.row_count == 0


def test_migrate_current_shard_is_idempotent(tmp_path: Path) -> None:
    shard = tmp_path / "current.parquet"
    row = {key: value for key, value in _v1_2_row().items() if key in POLYGON_PUBLIC_SCHEMA.names}
    row["schema_version"] = "v1.3"
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA), shard)
    before = hash_shard(shard)

    result = migrate_public_shard(shard)

    assert result.changed is False
    assert result.shard_sha256 == before
    assert hash_shard(shard) == before


def test_migrate_rejects_unknown_schema_without_changing_original(tmp_path: Path) -> None:
    shard = tmp_path / "unknown.parquet"
    pq.write_table(pa.table({"value": [1]}), shard)
    before = shard.read_bytes()

    with pytest.raises(ValueError, match="unsupported polygon schema"):
        migrate_public_shard(shard)

    assert shard.read_bytes() == before


def test_migration_private_batch_writer_sets_current_schema_version(tmp_path: Path) -> None:
    source = tmp_path / "source.parquet"
    staged = tmp_path / "staged.parquet"
    pq.write_table(pa.Table.from_pylist([_v1_2_row()], schema=POLYGON_PUBLIC_SCHEMA_V1_2), source)
    sink = BatchParquetSink(staged, POLYGON_PUBLIC_SCHEMA, batch_rows=1)
    try:
        _write_migration_batch(pq.ParquetFile(source).read_row_group(0), sink)
        sink.close()
        _validate_migrated_shard(staged, sink.row_count, 1)
    finally:
        sink.close()
    assert pq.read_table(staged).to_pylist()[0]["schema_version"] == "v1.3"
