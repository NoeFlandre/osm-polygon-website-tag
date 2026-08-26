"""Tests for durable language-detection checkpoint parts."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.language_detection_checkpoint as checkpoint
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA_V1_4
from osm_polygon_website_tag.pipeline.glotlid import ModelIdentity


def _model(sha256: str = "a" * 64) -> ModelIdentity:
    return ModelIdentity("cis-lmu/glotlid", "model_v3.bin", "85cd671", sha256)


def _row(index: int) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in POLYGON_PUBLIC_SCHEMA_V1_4:
        if field.name == "polygon_id":
            values[field.name] = f"source:way/{index}"
        elif field.name == "website":
            values[field.name] = "https://example.org"
        elif field.name == "has_website" or field.name == "has_any_website":
            values[field.name] = True
        elif field.name == "website_text":
            values[field.name] = f"text {index}"
        elif field.name == "website_word_count":
            values[field.name] = 2
        elif field.name == "website_text_status":
            values[field.name] = "success"
        elif field.name == "contact_website_text_status":
            values[field.name] = "absent"
        elif field.name == "schema_version":
            values[field.name] = "v1.4"
        elif field.name in {
            "contact_website",
            "website_language",
            "website_language_probability",
            "contact_website_language",
            "contact_website_language_probability",
        }:
            values[field.name] = None
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
    values["has_contact_website"] = False
    return values


def test_checkpoint_metadata_binds_source_and_model(tmp_path: Path) -> None:
    shard = tmp_path / "nested" / "region.parquet"
    checkpoint_state = checkpoint.load_language_checkpoint(
        shard,
        source_row_count=4,
        source_shard_sha256="b" * 64,
        model=_model(),
    )

    metadata = json.loads((checkpoint_state.directory / "checkpoint.json").read_text())

    assert metadata == {
        "checkpoint_version": 1,
        "schema_version": "v1.4",
        "source_row_count": 4,
        "source_shard_sha256": "b" * 64,
        "model_repository": "cis-lmu/glotlid",
        "model_filename": "model_v3.bin",
        "model_revision": "85cd671",
        "model_sha256": "a" * 64,
    }
    reloaded = checkpoint.load_language_checkpoint(
        shard,
        source_row_count=4,
        source_shard_sha256="b" * 64,
        model=_model(),
    )
    assert reloaded.completed_rows == 0


def test_checkpoint_rejects_source_or_model_drift(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    checkpoint.load_language_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=_model(),
    )

    with pytest.raises(ValueError, match="does not match"):
        checkpoint.load_language_checkpoint(
            shard,
            source_row_count=1,
            source_shard_sha256="c" * 64,
            model=_model(),
        )
    with pytest.raises(ValueError, match="does not match"):
        checkpoint.load_language_checkpoint(
            shard,
            source_row_count=1,
            source_shard_sha256="b" * 64,
            model=_model("d" * 64),
        )


def test_checkpoint_parts_are_sequential_and_v1_4_shaped(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    checkpoint.write_language_checkpoint_part(directory, 0, [_row(0)], batch_rows=1)

    parts = checkpoint.checkpoint_parts(directory)
    assert parts == (directory / "part-00000000.parquet",)
    assert pq.read_schema(parts[0]).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)

    (directory / "part-00000002.parquet").write_bytes(parts[0].read_bytes())
    with pytest.raises(ValueError, match="non-sequential"):
        checkpoint.checkpoint_parts(directory)


def test_checkpoint_allows_a_complete_durable_prefix(tmp_path: Path) -> None:
    shard = tmp_path / "nested" / "region.parquet"
    state = checkpoint.load_language_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=_model(),
    )
    checkpoint.write_language_checkpoint_part(state.directory, 0, [_row(0)], batch_rows=1)

    loaded = checkpoint.load_language_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=_model(),
    )

    assert loaded.completed_rows == 1


def test_checkpoint_assembly_preserves_part_order(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    checkpoint.write_language_checkpoint_part(directory, 0, [_row(0)], batch_rows=1)
    checkpoint.write_language_checkpoint_part(directory, 1, [_row(1)], batch_rows=1)

    staged = tmp_path / "staged.parquet"
    max_batch_rows = checkpoint.assemble_language_checkpoint(
        (directory / "part-00000000.parquet", directory / "part-00000001.parquet"),
        staged,
        batch_rows=1,
        row_count=2,
    )

    assert max_batch_rows == 1
    assert pq.ParquetFile(staged).metadata.row_group(0).column(0).compression == "SNAPPY"
    assert [row["polygon_id"] for row in pq.read_table(staged).to_pylist()] == [
        "source:way/0",
        "source:way/1",
    ]


def test_checkpoint_assembly_requests_snappy_compression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    checkpoint.write_language_checkpoint_part(directory, 0, [_row(0)], batch_rows=1)
    original_writer = checkpoint.pq.ParquetWriter
    compressions: list[object] = []

    def recording_writer(*args: object, **kwargs: object) -> object:
        compressions.append(kwargs.get("compression"))
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(checkpoint.pq, "ParquetWriter", recording_writer)

    checkpoint.assemble_language_checkpoint(
        (directory / "part-00000000.parquet",),
        tmp_path / "staged.parquet",
        batch_rows=1,
        row_count=1,
    )

    assert compressions == ["snappy"]


def test_empty_checkpoint_assembly_reports_no_batch_rows(tmp_path: Path) -> None:
    staged = tmp_path / "empty.parquet"

    assert checkpoint.assemble_language_checkpoint((), staged, batch_rows=1, row_count=0) == 0
    assert pq.read_table(staged).num_rows == 0


def test_checkpoint_rejects_unknown_files_and_cleans_known_temps(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    metadata = directory / "checkpoint.json"
    temporary = directory / ".part-00000000.parquet.writing"
    temporary.write_bytes(b"stale")
    metadata_temporary = metadata.with_suffix(metadata.suffix + ".tmp")
    metadata_temporary.write_text("stale")
    checkpoint._cleanup_checkpoint_temps(directory, metadata)
    assert not temporary.exists()
    assert not metadata_temporary.exists()
    checkpoint._cleanup_checkpoint_temps(directory, metadata)

    (directory / "unexpected").write_text("x")
    with pytest.raises(ValueError, match="unrecognized"):
        checkpoint._validate_checkpoint_contents(directory, ())


def test_checkpoint_cleanup_uses_missing_ok_for_known_temps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    metadata = directory / "checkpoint.json"
    temporary = directory / ".part-00000000.parquet.writing"
    temporary.write_bytes(b"stale")
    metadata_temporary = metadata.with_suffix(metadata.suffix + ".tmp")
    metadata_temporary.write_text("stale")
    calls: list[tuple[str, bool | None]] = []

    def record_unlink(path: Path, *, missing_ok: bool | None = False) -> None:
        calls.append((path.name, missing_ok))

    monkeypatch.setattr(checkpoint.Path, "unlink", record_unlink)

    checkpoint._cleanup_checkpoint_temps(directory, metadata)

    assert calls == [(temporary.name, True), (metadata_temporary.name, True)]
