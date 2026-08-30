"""Architecture contract for durable enrichment checkpoints."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.enrichment_checkpoint as checkpoint
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA


def test_enrichment_checkpoint_module_exposes_focused_boundary() -> None:
    """Checkpoint persistence is isolated from URL-enrichment orchestration."""
    module = importlib.import_module("osm_polygon_website_tag.pipeline.enrichment_checkpoint")

    assert set(module.__all__) == {
        "EnrichmentCheckpoint",
        "assemble_checkpoint",
        "checkpoint_parts",
        "load_checkpoint",
        "write_checkpoint_part",
    }


def _row(index: int) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in POLYGON_PUBLIC_SCHEMA:
        if field.name == "polygon_id":
            values[field.name] = f"source:way/{index}"
        elif pa.types.is_boolean(field.type):
            values[field.name] = False
        elif pa.types.is_integer(field.type):
            values[field.name] = 0
        elif pa.types.is_floating(field.type):
            values[field.name] = 0.0
        elif pa.types.is_timestamp(field.type):
            values[field.name] = pa.scalar(0, type=field.type).as_py()
        else:
            values[field.name] = ""
    values["has_any_website"] = True
    values["has_website"] = True
    values["website"] = "https://example.org"
    values["schema_version"] = "v1.3"
    return values


def test_checkpoint_path_helpers_are_source_scoped(tmp_path: Path) -> None:
    shard = tmp_path / "polygons" / "region.parquet"
    directory = checkpoint._checkpoint_directory(shard)
    assert directory == tmp_path / "polygons" / ".region.parquet.enriching.parts"
    assert checkpoint._checkpoint_part_path(directory, 3).name == "part-00000003.parquet"


def test_checkpoint_metadata_creation_cleanup_and_validation(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    metadata = directory / "checkpoint.json"
    (directory / ".part-00000000.parquet.writing").write_bytes(b"stale")
    metadata.with_suffix(".json.tmp").write_text("stale")

    checkpoint._cleanup_checkpoint_temps(directory, metadata)
    checkpoint._ensure_checkpoint_metadata(
        directory,
        metadata,
        shard=tmp_path / "region.parquet",
        source_row_count=2,
        source_shard_sha256="a" * 64,
    )
    assert json.loads(metadata.read_text()) == {
        "checkpoint_version": 1,
        "schema_version": "v1.3",
        "source_row_count": 2,
        "source_shard_sha256": "a" * 64,
    }
    checkpoint._ensure_checkpoint_metadata(
        directory,
        metadata,
        shard=tmp_path / "region.parquet",
        source_row_count=2,
        source_shard_sha256="a" * 64,
    )
    with pytest.raises(
        ValueError,
        match=re.escape("enrichment checkpoint does not match source shard: region.parquet"),
    ):
        checkpoint._ensure_checkpoint_metadata(
            directory,
            metadata,
            shard=tmp_path / "region.parquet",
            source_row_count=3,
            source_shard_sha256="a" * 64,
        )


def test_checkpoint_metadata_writer_persists_the_complete_contract(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()

    checkpoint._write_checkpoint_metadata(
        directory,
        source_row_count=7,
        source_shard_sha256="b" * 64,
        schema_version="v9",
    )

    assert json.loads((directory / "checkpoint.json").read_text()) == {
        "checkpoint_version": 1,
        "schema_version": "v9",
        "source_row_count": 7,
        "source_shard_sha256": "b" * 64,
    }


def test_load_checkpoint_creates_nested_directory_and_forwards_identity(
    tmp_path: Path,
) -> None:
    shard = tmp_path / "nested" / "polygons" / "region.parquet"

    loaded = checkpoint.load_checkpoint(
        shard,
        source_row_count=7,
        source_shard_sha256="b" * 64,
        schema_version="v9",
    )

    assert loaded.directory == checkpoint._checkpoint_directory(shard)
    assert loaded.parts == ()
    assert loaded.completed_rows == 0
    assert json.loads((loaded.directory / "checkpoint.json").read_text()) == {
        "checkpoint_version": 1,
        "schema_version": "v9",
        "source_row_count": 7,
        "source_shard_sha256": "b" * 64,
    }

    with pytest.raises(
        ValueError,
        match=re.escape("enrichment checkpoint does not match source shard: region.parquet"),
    ):
        checkpoint.load_checkpoint(
            shard,
            source_row_count=8,
            source_shard_sha256="b" * 64,
            schema_version="v9",
        )


def test_load_checkpoint_honors_schema_and_allows_a_complete_prefix(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    schema = POLYGON_PUBLIC_SCHEMA.with_metadata({b"checkpoint": b"custom"})
    state = checkpoint.load_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="a" * 64,
        schema=schema,
    )
    checkpoint.write_checkpoint_part(
        state.directory,
        0,
        [_row(1)],
        batch_rows=1,
        schema=schema,
    )

    loaded = checkpoint.load_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="a" * 64,
        schema=schema,
    )

    assert loaded.parts == (state.directory / "part-00000000.parquet",)
    assert loaded.completed_rows == 1


def test_checkpoint_parts_validate_sequence_and_schema(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    checkpoint._write_checkpoint_metadata(
        directory,
        source_row_count=1,
        source_shard_sha256="a" * 64,
    )
    part = directory / "part-00000000.parquet"
    pq.write_table(pa.Table.from_pylist([_row(1)], schema=POLYGON_PUBLIC_SCHEMA), part)
    assert checkpoint.checkpoint_parts(directory) == (part,)

    (directory / "part-00000002.parquet").write_bytes(part.read_bytes())
    with pytest.raises(ValueError, match="non-sequential"):
        checkpoint.checkpoint_parts(directory)


def test_checkpoint_parts_preserve_enrichment_error_identity(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    wrong_schema = POLYGON_PUBLIC_SCHEMA.append(pa.field("unexpected", pa.string()))
    row = {**_row(1), "unexpected": "value"}
    pq.write_table(
        pa.Table.from_pylist([row], schema=wrong_schema),
        directory / "part-00000000.parquet",
    )

    with pytest.raises(ValueError, match="invalid enrichment checkpoint schema"):
        checkpoint.checkpoint_parts(directory)


def test_checkpoint_row_writing_and_validation_are_bounded(tmp_path: Path) -> None:
    class Sink:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        def add(self, row: dict[str, object]) -> None:
            self.rows.append(row)

    sink: Any = Sink()
    checkpoint._write_checkpoint_rows(sink, [_row(1), _row(2)])
    assert [row["polygon_id"] for row in sink.rows] == ["source:way/1", "source:way/2"]

    valid = tmp_path / "valid.parquet"
    pq.write_table(pa.Table.from_pylist([_row(1)], schema=POLYGON_PUBLIC_SCHEMA), valid)
    checkpoint._validate_checkpoint_part(valid, actual_rows=1, expected_rows=1)
    with pytest.raises(ValueError, match="enrichment checkpoint row count changed"):
        checkpoint._validate_checkpoint_part(valid, actual_rows=0, expected_rows=1)


def test_checkpoint_part_writer_preserves_enrichment_error_identity(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    checkpoint.write_checkpoint_part(directory, 0, [_row(1)], batch_rows=1)

    with pytest.raises(
        ValueError,
        match=re.escape("enrichment checkpoint part already exists: part-00000000.parquet"),
    ):
        checkpoint.write_checkpoint_part(directory, 0, [_row(2)], batch_rows=1)


def test_checkpoint_assembly_preserves_enrichment_error_identity(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    checkpoint.write_checkpoint_part(directory, 0, [_row(1)], batch_rows=1)
    staged = tmp_path / "staged.parquet"

    with pytest.raises(
        ValueError,
        match="enrichment row count changed while assembling checkpoint",
    ):
        checkpoint.assemble_checkpoint(
            (directory / "part-00000000.parquet",),
            staged,
            batch_rows=1,
            row_count=2,
        )


def test_checkpoint_contents_reject_unknown_files(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    (directory / "checkpoint.json").write_text("{}")
    (directory / "unexpected").write_text("x")
    with pytest.raises(ValueError, match="unrecognized enrichment checkpoint contents"):
        checkpoint._validate_checkpoint_contents(directory, ())
