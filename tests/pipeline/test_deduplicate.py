"""Tests for global canonicalization of public polygon shards."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import polygon_row_v1_3

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.pipeline import deduplicate as deduplicate_module
from osm_polygon_website_tag.pipeline.deduplicate import (
    _materialise_partitions,
    _scalar_count,
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
    contact: str | None = None,
) -> dict[str, object]:
    stem = source_pbf.removesuffix(".osm.pbf")
    row = polygon_row_v1_3(
        polygon_id=f"{stem}:way/{osm_id}",
        website=website,
        contact=contact,
        source_pbf=source_pbf,
        osm_id=osm_id,
        osm_version=osm_version,
        osm_timestamp=dt.datetime(2026, 1, timestamp_day, tzinfo=dt.UTC),
        website_text=f"text from {website}",
        website_word_count=3,
        website_text_status="success",
    )
    return {field.name: row[field.name] for field in POLYGON_PUBLIC_SCHEMA}


def _write_shard(
    path: Path,
    rows: list[dict[str, object]],
    *,
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


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


def test_deduplicate_public_shards_preserves_v1_4_language_fields(tmp_path: Path) -> None:
    source_dir = tmp_path / "polygons"
    row = _row(
        source_pbf=SOURCE_NAMES[0],
        osm_id=11,
        osm_version=1,
        website="https://alpha.example",
        timestamp_day=1,
    )
    row.update(
        {
            "website_language": "eng_Latn",
            "website_language_probability": 0.9,
            "contact_website_language": None,
            "contact_website_language_probability": None,
        }
    )
    _write_shard(source_dir / "alpha-latest.parquet", [row], schema=POLYGON_PUBLIC_SCHEMA_V1_4)

    deduplicate_public_shards(
        source_dir,
        tmp_path / "canonical",
        source_names=(SOURCE_NAMES[0],),
    )

    output = tmp_path / "canonical" / "alpha-latest.parquet"
    assert pq.read_schema(output).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
    assert pq.read_table(output)["website_language"].to_pylist() == ["eng_Latn"]


def test_deduplicate_summary_counts_contact_conflicts_and_expected_sources(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "polygons"
    rows = [
        _row(
            source_pbf=SOURCE_NAMES[0],
            osm_id=15,
            osm_version=2,
            website="https://same.example",
            timestamp_day=2,
        ),
        _row(
            source_pbf=SOURCE_NAMES[1],
            osm_id=15,
            osm_version=1,
            website="https://same.example",
            timestamp_day=1,
            contact="https://contact.example",
        ),
    ]
    _write_shard(source_dir / "alpha-latest.parquet", [rows[0]])
    _write_shard(source_dir / "beta-latest.parquet", [rows[1]])

    result = deduplicate_public_shards(
        source_dir,
        tmp_path / "canonical",
        source_names=(*SOURCE_NAMES, "empty-latest.osm.pbf"),
    )

    assert result.input_row_count == 2
    assert result.output_row_count == 1
    assert result.duplicate_group_count == 1
    assert result.duplicate_extra_row_count == 1
    assert result.website_conflict_group_count == 0
    assert result.contact_website_conflict_group_count == 1
    assert result.source_count == 3
    assert result.output_counts_by_source == {
        SOURCE_NAMES[0]: 1,
        SOURCE_NAMES[1]: 0,
        "empty-latest.osm.pbf": 0,
    }
    assert (tmp_path / "canonical" / "empty-latest.parquet").exists()


def test_deduplicate_infers_source_names_when_inventory_is_omitted(tmp_path: Path) -> None:
    source_dir = tmp_path / "polygons"
    row = _row(
        source_pbf=SOURCE_NAMES[0],
        osm_id=21,
        osm_version=1,
        website="https://alpha.example",
        timestamp_day=1,
    )
    _write_shard(source_dir / "alpha-latest.parquet", [row])

    result = deduplicate_public_shards(source_dir, tmp_path / "canonical")

    assert result.source_count == 1
    assert result.output_counts_by_source == {SOURCE_NAMES[0]: 1}
    assert pq.read_table(tmp_path / "canonical" / "alpha-latest.parquet").num_rows == 1


def test_deduplicate_escapes_quotes_and_creates_nested_output_parent(tmp_path: Path) -> None:
    source_dir = tmp_path / "source's polygons"
    output_dir = tmp_path / "new parent" / "canonical's"
    row = _row(
        source_pbf=SOURCE_NAMES[0],
        osm_id=22,
        osm_version=1,
        website="https://alpha.example",
        timestamp_day=1,
    )
    _write_shard(source_dir / "alpha-latest.parquet", [row])

    result = deduplicate_public_shards(source_dir, output_dir)

    assert result.output_row_count == 1
    assert pq.read_table(output_dir / "alpha-latest.parquet").num_rows == 1


def test_deduplicate_stages_in_output_parent_for_atomic_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "polygons"
    output_dir = tmp_path / "nested" / "canonical"
    row = _row(
        source_pbf=SOURCE_NAMES[0],
        osm_id=25,
        osm_version=1,
        website="https://alpha.example",
        timestamp_day=1,
    )
    _write_shard(source_dir / "alpha-latest.parquet", [row])
    original_mkdtemp = deduplicate_module.tempfile.mkdtemp
    staging_parents: list[Path | None] = []

    def record_mkdtemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        staging_parents.append(Path(dir) if dir is not None else None)
        return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

    monkeypatch.setattr(deduplicate_module.tempfile, "mkdtemp", record_mkdtemp)

    deduplicate_public_shards(source_dir, output_dir)

    assert staging_parents == [output_dir.parent]


def test_deduplicate_rejects_unlisted_source_and_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "polygons"
    row = _row(
        source_pbf="unlisted-latest.osm.pbf",
        osm_id=23,
        osm_version=1,
        website="https://unlisted.example",
        timestamp_day=1,
    )
    _write_shard(source_dir / "unlisted-latest.parquet", [row])

    connection = duckdb.connect()

    class TrackingConnection:
        def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
            self.inner = inner
            self.closed = False

        def execute(self, query: str) -> duckdb.DuckDBPyConnection:
            return self.inner.execute(query)

        def close(self) -> None:
            self.closed = True
            self.inner.close()

    tracked = TrackingConnection(connection)
    monkeypatch.setattr(
        deduplicate_module.duckdb,
        "connect",
        lambda: cast(duckdb.DuckDBPyConnection, tracked),
    )

    with pytest.raises(ValueError) as error:
        deduplicate_public_shards(
            source_dir,
            tmp_path / "canonical",
            source_names=(SOURCE_NAMES[0],),
        )

    assert str(error.value) == (
        "source shards contain unlisted source PBFs: unlisted-latest.osm.pbf"
    )
    assert tracked.closed


def test_deduplicate_cleanup_error_does_not_mask_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "polygons"
    row = _row(
        source_pbf=SOURCE_NAMES[0],
        osm_id=24,
        osm_version=1,
        website="https://alpha.example",
        timestamp_day=1,
    )
    _write_shard(source_dir / "alpha-latest.parquet", [row])
    output_dir = tmp_path / "canonical"
    original_remove_tree = deduplicate_module._remove_tree

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("canonical shard write failed")

    def fail_staging_cleanup(path: Path) -> None:
        if path.name.startswith(f".{output_dir.name}-"):
            raise OSError("staging cleanup failed")
        original_remove_tree(path)

    monkeypatch.setattr(deduplicate_module, "_write_canonical_shards", fail_write)
    monkeypatch.setattr(deduplicate_module, "_remove_tree", fail_staging_cleanup)

    with pytest.raises(RuntimeError, match="canonical shard write failed"):
        deduplicate_public_shards(source_dir, output_dir)


def test_materialise_partitions_sorts_two_rows_and_writes_snappy(tmp_path: Path) -> None:
    source_name = SOURCE_NAMES[0]
    staging_dir = tmp_path / "staging"
    partition_dir = staging_dir / "partitions"
    rows = [
        _row(
            source_pbf=source_name,
            osm_id=2,
            osm_version=1,
            website="https://two.example",
            timestamp_day=1,
        ),
        _row(
            source_pbf=source_name,
            osm_id=1,
            osm_version=1,
            website="https://one.example",
            timestamp_day=1,
        ),
    ]
    _write_shard(partition_dir / f"source_pbf={source_name}" / "part-0.parquet", rows)

    _materialise_partitions(
        partition_dir,
        staging_dir,
        (source_name,),
        target_schema=POLYGON_PUBLIC_SCHEMA,
    )

    output = staging_dir / "alpha-latest.parquet"
    table = pq.read_table(output)
    assert table["osm_id"].to_pylist() == [1, 2]
    assert pq.ParquetFile(output).metadata.row_group(0).column(0).compression == "SNAPPY"


def test_materialise_partitions_promotes_null_only_column_parts(tmp_path: Path) -> None:
    source_name = SOURCE_NAMES[0]
    staging_dir = tmp_path / "staging"
    partition_dir = staging_dir / "partitions"
    null_website_schema = POLYGON_PUBLIC_SCHEMA.set(
        POLYGON_PUBLIC_SCHEMA.get_field_index("website"),
        pa.field("website", pa.null(), nullable=True),
    )
    null_website_row = _row(
        source_pbf=source_name,
        osm_id=26,
        osm_version=1,
        website="https://unused.example",
        timestamp_day=1,
    )
    null_website_row["website"] = None
    text_website_row = _row(
        source_pbf=source_name,
        osm_id=27,
        osm_version=1,
        website="https://example.org",
        timestamp_day=1,
    )
    partition = partition_dir / f"source_pbf={source_name}"
    _write_shard(partition / "part-0.parquet", [null_website_row], schema=null_website_schema)
    _write_shard(partition / "part-1.parquet", [text_website_row])

    _materialise_partitions(
        partition_dir,
        staging_dir,
        (source_name,),
        target_schema=POLYGON_PUBLIC_SCHEMA,
    )

    output = pq.read_table(staging_dir / "alpha-latest.parquet")
    assert output.schema.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert output["website"].to_pylist() == [None, "https://example.org"]


def test_scalar_count_returns_zero_for_empty_result() -> None:
    connection = duckdb.connect()
    try:
        assert _scalar_count(connection, "SELECT 1 WHERE FALSE") == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    "names",
    [(), ("alpha-latest.osm.pbf", "alpha-latest.osm.pbf"), ("alpha-latest.parquet",)],
)
def test_validate_source_names_rejects_invalid_inventories(
    names: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="source_names"):
        _validate_source_names(names)


def test_validate_source_names_reports_exact_inventory_and_suffix_errors() -> None:
    with pytest.raises(ValueError) as inventory_error:
        _validate_source_names(())
    assert str(inventory_error.value) == (
        "source_names must contain at least one unique source PBF name"
    )

    with pytest.raises(ValueError) as suffix_error:
        _validate_source_names(("alpha-latest.parquet",))
    assert str(suffix_error.value) == "source_names must end with '.osm.pbf'"
