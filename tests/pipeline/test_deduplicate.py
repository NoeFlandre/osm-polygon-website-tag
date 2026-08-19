"""Tests for global canonicalization of public polygon shards."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import legacy_polygon_row

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields
from osm_polygon_website_tag.pipeline.deduplicate import (
    _validate_source_names,
    deduplicate_public_shards,
)

SOURCE_NAMES = ("alpha-latest.osm.pbf", "beta-latest.osm.pbf")


def _row(
    *,
    source_pbf: str,
    osm_id: int,
    osm_version: int,
    website: str,
    timestamp_day: int,
) -> dict[str, object]:
    stem = source_pbf.removesuffix(".osm.pbf")
    row = legacy_polygon_row(
        polygon_id=f"{stem}:way/{osm_id}",
        website=website,
        contact=None,
    )
    for field in (
        "preferred_website",
        "preferred_website_source",
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "area_km2",
    ):
        row.pop(field)
    row.update(initial_text_fields(website_present=True, contact_website_present=False))
    row.update(
        {
            "source_pbf": source_pbf,
            "osm_id": osm_id,
            "osm_version": osm_version,
            "osm_timestamp": dt.datetime(2026, 1, timestamp_day, tzinfo=dt.UTC),
            "website_text": f"text from {website}",
            "website_word_count": 3,
            "website_text_status": "success",
            "schema_version": "v1.3",
        }
    )
    return {field.name: row[field.name] for field in POLYGON_PUBLIC_SCHEMA}


def _write_shard(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), path)


def test_deduplicate_public_shards_keeps_latest_row_and_empty_source_shards(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "polygons"
    old = _row(
        source_pbf=SOURCE_NAMES[0],
        osm_id=7,
        osm_version=1,
        website="https://old.example",
        timestamp_day=1,
    )
    new = _row(
        source_pbf=SOURCE_NAMES[1],
        osm_id=7,
        osm_version=2,
        website="https://new.example",
        timestamp_day=2,
    )
    _write_shard(source_dir / "alpha-latest.parquet", [old])
    _write_shard(source_dir / "beta-latest.parquet", [new])

    result = deduplicate_public_shards(
        source_dir,
        tmp_path / "canonical",
        source_names=SOURCE_NAMES,
    )

    assert result.input_row_count == 2
    assert result.output_row_count == 1
    assert result.duplicate_group_count == 1
    assert result.duplicate_extra_row_count == 1
    assert result.website_conflict_group_count == 1
    assert result.output_counts_by_source == {
        "alpha-latest.osm.pbf": 0,
        "beta-latest.osm.pbf": 1,
    }
    assert pq.read_schema(tmp_path / "canonical" / "alpha-latest.parquet").equals(
        POLYGON_PUBLIC_SCHEMA,
        check_metadata=True,
    )
    rows = pq.read_table(tmp_path / "canonical" / "beta-latest.parquet").to_pylist()
    assert [row["website"] for row in rows] == ["https://new.example"]
    assert pq.read_table(tmp_path / "canonical" / "alpha-latest.parquet").num_rows == 0


def test_deduplicate_public_shards_uses_source_name_for_exact_ties(tmp_path: Path) -> None:
    source_dir = tmp_path / "polygons"
    rows = [
        _row(
            source_pbf=SOURCE_NAMES[1],
            osm_id=9,
            osm_version=3,
            website="https://beta.example",
            timestamp_day=3,
        ),
        _row(
            source_pbf=SOURCE_NAMES[0],
            osm_id=9,
            osm_version=3,
            website="https://alpha.example",
            timestamp_day=3,
        ),
    ]
    _write_shard(source_dir / "alpha-latest.parquet", [rows[1]])
    _write_shard(source_dir / "beta-latest.parquet", [rows[0]])

    deduplicate_public_shards(
        source_dir,
        tmp_path / "canonical",
        source_names=SOURCE_NAMES,
    )

    assert (
        pq.read_table(tmp_path / "canonical" / "alpha-latest.parquet").to_pylist()[0]["website"]
        == "https://alpha.example"
    )
    assert pq.read_table(tmp_path / "canonical" / "beta-latest.parquet").num_rows == 0
    assert not (tmp_path / "canonical" / "partitions").exists()
    assert not (tmp_path / "canonical" / "duckdb-temp").exists()


@pytest.mark.parametrize(
    "names",
    [(), ("alpha-latest.osm.pbf", "alpha-latest.osm.pbf"), ("alpha-latest.parquet",)],
)
def test_validate_source_names_rejects_invalid_inventories(
    names: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="source_names"):
        _validate_source_names(names)
