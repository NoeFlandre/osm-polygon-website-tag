"""Tests for the durable checkpoint store shared by resumable stages."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.checkpoint_storage as checkpoint_storage
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint, CheckpointStore

_SCHEMA = pa.schema([pa.field("value", pa.int64())])
_DRIFTED_SCHEMA = pa.schema([pa.field("value", pa.int64())], metadata={b"stage": b"drifted"})
_STORE = CheckpointStore(
    label="test",
    directory_suffix=".test.parts",
    schema=_SCHEMA,
    schema_version="v9",
    identity_description="source identity",
)


def _metadata(directory: Path) -> dict[str, object]:
    payload = json.loads((directory / "checkpoint.json").read_text())
    assert isinstance(payload, dict)
    return payload


def test_store_module_exposes_a_focused_boundary() -> None:
    """Callers work in whole checkpoints, never in part-file mechanics."""
    assert set(checkpoint_storage.__all__) == {
        "CHECKPOINT_METADATA_NAME",
        "CHECKPOINT_VERSION",
        "Checkpoint",
        "CheckpointStore",
    }


def test_checkpoint_directory_is_source_scoped_and_hidden(tmp_path: Path) -> None:
    shard = tmp_path / "polygons" / "region.parquet"

    assert _STORE.directory_for(shard) == tmp_path / "polygons" / ".region.parquet.test.parts"


def test_load_creates_nested_directory_and_binds_source_identity(tmp_path: Path) -> None:
    shard = tmp_path / "nested" / "polygons" / "region.parquet"

    loaded = _STORE.load(shard, source_row_count=7, source_shard_sha256="b" * 64)

    assert loaded == Checkpoint(_STORE.directory_for(shard), (), 0)
    assert _metadata(loaded.directory) == {
        "checkpoint_version": 1,
        "schema_version": "v9",
        "source_row_count": 7,
        "source_shard_sha256": "b" * 64,
    }


def test_load_extends_the_shared_contract_with_stage_identity(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"

    loaded = _STORE.load(
        shard,
        source_row_count=1,
        source_shard_sha256="a" * 64,
        identity={"model_sha256": "c" * 64},
    )

    assert _metadata(loaded.directory) == {
        "checkpoint_version": 1,
        "schema_version": "v9",
        "source_row_count": 1,
        "source_shard_sha256": "a" * 64,
        "model_sha256": "c" * 64,
    }
    with pytest.raises(ValueError, match="test checkpoint does not match source identity"):
        _STORE.load(
            shard,
            source_row_count=1,
            source_shard_sha256="a" * 64,
            identity={"model_sha256": "d" * 64},
        )


def test_load_reuses_a_matching_contract_and_rejects_drift(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)

    reloaded = _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)
    assert reloaded.completed_rows == 0

    with pytest.raises(
        ValueError,
        match=re.escape("test checkpoint does not match source identity: region.parquet"),
    ):
        _STORE.load(shard, source_row_count=1, source_shard_sha256="d" * 64)


def test_load_rejects_a_populated_directory_without_a_contract(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    directory = _STORE.directory_for(shard)
    directory.mkdir()
    (directory / "unexpected").write_text("x")

    with pytest.raises(ValueError, match="unrecognized test checkpoint contents"):
        _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)


def test_load_rejects_unknown_files_beside_a_bound_contract(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    loaded = _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)
    (loaded.directory / "unexpected").write_text("x")

    with pytest.raises(
        ValueError, match=r"unrecognized test checkpoint contents: \['unexpected'\]"
    ):
        _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)


def test_load_clears_only_known_temporaries(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    directory = _STORE.directory_for(shard)
    directory.mkdir()
    writing = directory / ".part-00000000.parquet.writing"
    writing.write_bytes(b"stale")
    metadata_temporary = directory / "checkpoint.json.tmp"
    metadata_temporary.write_text("stale")

    loaded = _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)

    assert not writing.exists()
    assert not metadata_temporary.exists()
    assert loaded.parts == ()


def test_load_tolerates_absent_temporaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shard = tmp_path / "region.parquet"
    directory = _STORE.directory_for(shard)
    directory.mkdir()
    (directory / ".part-00000000.parquet.writing").write_bytes(b"stale")
    (directory / "checkpoint.json.tmp").write_text("stale")
    calls: list[tuple[str, bool | None]] = []
    original_unlink = Path.unlink

    def record_unlink(path: Path, *, missing_ok: bool = False) -> None:
        calls.append((path.name, missing_ok))
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(checkpoint_storage.Path, "unlink", record_unlink)

    _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)

    assert calls == [(".part-00000000.parquet.writing", True), ("checkpoint.json.tmp", True)]


def test_load_reports_the_durable_prefix(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    opened = _STORE.load(shard, source_row_count=3, source_shard_sha256="a" * 64)
    _STORE.write_part(opened.directory, 0, [{"value": 1}, {"value": 2}], batch_rows=2)

    loaded = _STORE.load(shard, source_row_count=3, source_shard_sha256="a" * 64)

    assert loaded.parts == (opened.directory / "part-00000000.parquet",)
    assert loaded.completed_rows == 2


def test_load_rejects_a_prefix_longer_than_its_source(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    opened = _STORE.load(shard, source_row_count=2, source_shard_sha256="a" * 64)
    _STORE.write_part(opened.directory, 0, [{"value": 1}, {"value": 2}], batch_rows=2)
    _STORE.write_part(opened.directory, 1, [{"value": 3}], batch_rows=1)

    with pytest.raises(
        ValueError,
        match=re.escape("test checkpoint exceeds source row count: region.parquet"),
    ):
        _STORE.load(shard, source_row_count=2, source_shard_sha256="a" * 64)


def test_parts_are_sequential_non_empty_and_schema_bound(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    _STORE.write_part(directory, 0, [{"value": 0}], batch_rows=1)
    _STORE.write_part(directory, 1, [{"value": 1}], batch_rows=1)

    assert _STORE.parts(directory) == (
        directory / "part-00000000.parquet",
        directory / "part-00000001.parquet",
    )

    gap = directory / "part-00000003.parquet"
    gap.write_bytes((directory / "part-00000000.parquet").read_bytes())
    with pytest.raises(ValueError, match="non-sequential test checkpoint part: part-00000003"):
        _STORE.parts(directory)
    gap.unlink()

    tagged = pa.schema([pa.field("value", pa.int64())], metadata={b"stage": b"test"})
    pq.write_table(
        pa.Table.from_pydict({"value": [1]}, schema=tagged),
        directory / "part-00000001.parquet",
    )
    with pytest.raises(ValueError, match="invalid test checkpoint schema: part-00000001"):
        _STORE.parts(directory)


def test_parts_reject_an_empty_durable_part(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    pq.write_table(
        pa.Table.from_pydict({"value": []}, schema=_SCHEMA),
        directory / "part-00000000.parquet",
    )

    with pytest.raises(ValueError, match="empty test checkpoint part: part-00000000"):
        _STORE.parts(directory)


def test_write_part_skips_an_empty_batch(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()

    _STORE.write_part(directory, 0, [], batch_rows=1)

    assert list(directory.iterdir()) == []


def test_write_part_publishes_rows_in_order_and_refuses_to_overwrite(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()

    _STORE.write_part(directory, 2, [{"value": 5}, {"value": 6}], batch_rows=1)

    part = directory / "part-00000002.parquet"
    assert [row["value"] for row in pq.read_table(part).to_pylist()] == [5, 6]
    with pytest.raises(
        ValueError,
        match=re.escape("test checkpoint part already exists: part-00000002.parquet"),
    ):
        _STORE.write_part(directory, 2, [{"value": 7}], batch_rows=1)


def test_write_part_removes_its_temporary_when_the_written_schema_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    monkeypatch.setattr(checkpoint_storage.pq, "read_schema", lambda path: _DRIFTED_SCHEMA)

    with pytest.raises(ValueError, match="test checkpoint schema mismatch"):
        _STORE.write_part(directory, 0, [{"value": 1}], batch_rows=1)

    assert list(directory.iterdir()) == []


def test_write_part_rejects_a_row_count_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    monkeypatch.setattr(checkpoint_storage.BatchParquetSink, "add", lambda self, row: None)

    with pytest.raises(ValueError, match="test checkpoint row count changed"):
        _STORE.write_part(directory, 0, [{"value": 1}], batch_rows=1)

    assert list(directory.iterdir()) == []


def test_assemble_streams_parts_in_order_under_snappy(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    _STORE.write_part(directory, 0, [{"value": 0}, {"value": 1}], batch_rows=2)
    _STORE.write_part(directory, 1, [{"value": 2}], batch_rows=2)
    staged = tmp_path / "staged.parquet"

    max_batch_rows = _STORE.assemble(_STORE.parts(directory), staged, batch_rows=2, row_count=3)

    assert max_batch_rows == 2
    assert [row["value"] for row in pq.read_table(staged).to_pylist()] == [0, 1, 2]
    assert pq.ParquetFile(staged).metadata.row_group(0).column(0).compression == "SNAPPY"


def test_assemble_requests_snappy_compression_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compression is named explicitly, never inherited from a library default."""
    directory = tmp_path / "parts"
    directory.mkdir()
    _STORE.write_part(directory, 0, [{"value": 0}], batch_rows=1)
    original_writer = checkpoint_storage.pq.ParquetWriter
    compressions: list[object] = []

    def recording_writer(*args: object, **kwargs: object) -> object:
        compressions.append(kwargs.get("compression"))
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(checkpoint_storage.pq, "ParquetWriter", recording_writer)

    _STORE.assemble(
        _STORE.parts(directory),
        tmp_path / "staged.parquet",
        batch_rows=1,
        row_count=1,
    )

    assert compressions == ["snappy"]


def test_assemble_writes_an_empty_shard_for_an_empty_prefix(tmp_path: Path) -> None:
    staged = tmp_path / "staged.parquet"

    assert _STORE.assemble((), staged, batch_rows=1, row_count=0) == 0
    assert pq.read_table(staged).num_rows == 0


def test_assemble_discards_a_staged_file_whose_row_count_changed(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    _STORE.write_part(directory, 0, [{"value": 0}], batch_rows=1)
    staged = tmp_path / "staged.parquet"

    with pytest.raises(ValueError, match="test row count changed while assembling checkpoint"):
        _STORE.assemble(_STORE.parts(directory), staged, batch_rows=1, row_count=2)

    assert not staged.exists()


def test_assemble_discards_a_staged_file_whose_schema_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = tmp_path / "staged.parquet"
    monkeypatch.setattr(checkpoint_storage.pq, "read_schema", lambda path: _DRIFTED_SCHEMA)

    with pytest.raises(ValueError, match="assembled test schema mismatch"):
        _STORE.assemble((), staged, batch_rows=1, row_count=0)

    assert not staged.exists()


def test_assemble_preserves_a_writer_failure_without_a_staged_file(
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
    staged = tmp_path / "never-created.parquet"

    with pytest.raises(ValueError, match="writer failed"):
        _STORE.assemble((), staged, batch_rows=1, row_count=0)

    assert not staged.exists()


def test_metadata_is_persisted_through_the_shared_atomic_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, dict[str, object]]] = []

    def record_write(path: Path, payload: dict[str, object]) -> None:
        calls.append((path, payload))

    monkeypatch.setattr(checkpoint_storage, "atomic_write_json", record_write)
    shard = tmp_path / "region.parquet"

    _STORE.load(shard, source_row_count=1, source_shard_sha256="a" * 64)

    directory = _STORE.directory_for(shard)
    assert calls == [
        (
            directory / "checkpoint.json",
            {
                "checkpoint_version": 1,
                "schema_version": "v9",
                "source_row_count": 1,
                "source_shard_sha256": "a" * 64,
            },
        )
    ]
