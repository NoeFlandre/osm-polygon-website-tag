"""Tests for shared bounded checkpoint mechanics."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.checkpoint_storage as checkpoint_storage
from osm_polygon_website_tag.pipeline.checkpoint_storage import (
    assemble_checkpoint,
    checkpoint_directory,
    checkpoint_part_path,
    ensure_checkpoint_metadata,
    validate_assembled_checkpoint,
    validate_checkpoint_part,
    validate_checkpoint_parts,
    write_checkpoint_metadata,
    write_checkpoint_part,
)


def test_checkpoint_part_path_uses_zero_padded_source_order(tmp_path: Path) -> None:
    assert checkpoint_part_path(tmp_path, 7).name == "part-00000007.parquet"


def test_checkpoint_directory_is_source_scoped(tmp_path: Path) -> None:
    shard = tmp_path / "polygons" / "region.parquet"

    assert checkpoint_directory(shard, ".language.parts") == (
        tmp_path / "polygons" / ".region.parquet.language.parts"
    )


def test_checkpoint_metadata_is_created_from_the_expected_contract(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    metadata_path = directory / "checkpoint.json"
    expected = {"checkpoint_version": 1, "source_row_count": 2}

    ensure_checkpoint_metadata(
        directory,
        metadata_path,
        shard=tmp_path / "region.parquet",
        expected=expected,
        label="test",
        mismatch_description="source identity",
    )

    assert json.loads(metadata_path.read_text()) == expected


def test_checkpoint_metadata_reuses_matching_existing_contract(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    metadata_path = directory / "checkpoint.json"
    expected = {"checkpoint_version": 1, "source_row_count": 2}
    metadata_path.write_text(json.dumps(expected), encoding="utf-8")

    ensure_checkpoint_metadata(
        directory,
        metadata_path,
        shard=tmp_path / "region.parquet",
        expected=expected,
        label="test",
        mismatch_description="source identity",
    )


def test_checkpoint_metadata_reports_existing_contract_drift(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    metadata_path = directory / "checkpoint.json"
    metadata_path.write_text(json.dumps({"checkpoint_version": 1}), encoding="utf-8")

    with pytest.raises(ValueError, match="test checkpoint does not match source identity"):
        ensure_checkpoint_metadata(
            directory,
            metadata_path,
            shard=tmp_path / "region.parquet",
            expected={"checkpoint_version": 2},
            label="test",
            mismatch_description="source identity",
        )


def test_checkpoint_parts_are_name_sorted_and_metadata_strict(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    schema = pa.schema([pa.field("value", pa.int64())])
    part_zero = directory / "part-00000000.parquet"
    part_one = directory / "part-00000001.parquet"
    pq.write_table(pa.Table.from_pydict({"value": [0]}, schema=schema), part_zero)
    pq.write_table(pa.Table.from_pydict({"value": [1]}, schema=schema), part_one)

    assert validate_checkpoint_parts(directory, schema=schema, label="test") == (
        part_zero,
        part_one,
    )

    metadata_schema = pa.schema([pa.field("value", pa.int64())], metadata={b"stage": b"test"})
    pq.write_table(pa.Table.from_pydict({"value": [1]}, schema=metadata_schema), part_one)
    with pytest.raises(ValueError, match="invalid test checkpoint schema"):
        validate_checkpoint_parts(directory, schema=schema, label="test")


def test_checkpoint_part_rejects_schema_metadata_drift(tmp_path: Path) -> None:
    path = tmp_path / "part.parquet"
    schema = pa.schema([pa.field("value", pa.int64())])
    actual_schema = pa.schema([pa.field("value", pa.int64())], metadata={b"stage": b"test"})
    pq.write_table(pa.Table.from_pydict({"value": [1]}, schema=actual_schema), path)

    with pytest.raises(ValueError, match="test checkpoint schema mismatch"):
        validate_checkpoint_part(path, 1, 1, schema=schema, label="test")


def test_write_checkpoint_part_preserves_validation_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = pa.schema([pa.field("value", pa.int64())])

    def reject_validation(
        path: Path,
        actual_rows: int,
        expected_rows: int,
        *,
        schema: pa.Schema,
        label: str,
    ) -> None:
        del path, actual_rows, expected_rows, schema
        raise ValueError(f"{label} checkpoint validation")

    monkeypatch.setattr(checkpoint_storage, "validate_checkpoint_part", reject_validation)

    with pytest.raises(ValueError, match="test checkpoint validation"):
        write_checkpoint_part(
            tmp_path,
            0,
            [{"value": 1}],
            batch_rows=1,
            schema=schema,
            label="test",
        )


def test_assembly_and_final_validation_preserve_labels(tmp_path: Path) -> None:
    schema = pa.schema([pa.field("value", pa.int64())])
    staged = tmp_path / "staged.parquet"

    with pytest.raises(ValueError, match="test row count changed while assembling checkpoint"):
        assemble_checkpoint(
            (),
            staged,
            batch_rows=1,
            row_count=1,
            schema=schema,
            label="test",
        )
    assert not staged.exists()

    actual_schema = pa.schema([pa.field("value", pa.int64())], metadata={b"stage": b"test"})
    pq.write_table(pa.Table.from_pydict({"value": [1]}, schema=actual_schema), staged)
    with pytest.raises(ValueError, match="assembled test schema mismatch"):
        validate_assembled_checkpoint(staged, 1, 1, schema=schema, label="test")


def test_assembly_cleanup_preserves_an_error_when_staged_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingWriter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def __enter__(self) -> FailingWriter:
            raise ValueError("writer failed")

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(checkpoint_storage.pq, "ParquetWriter", FailingWriter)

    with pytest.raises(ValueError, match="writer failed"):
        assemble_checkpoint(
            (),
            tmp_path / "never-created.parquet",
            batch_rows=1,
            row_count=0,
            schema=pa.schema([pa.field("value", pa.int64())]),
            label="test",
        )


def test_write_checkpoint_metadata_uses_the_shared_atomic_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, dict[str, object]]] = []

    def record_write(path: Path, payload: dict[str, object]) -> None:
        calls.append((path, payload))

    monkeypatch.setattr(checkpoint_storage, "atomic_write_json", record_write)
    payload: dict[str, object] = {"checkpoint_version": 1}

    write_checkpoint_metadata(tmp_path, payload)

    assert calls == [(tmp_path / "checkpoint.json", payload)]
