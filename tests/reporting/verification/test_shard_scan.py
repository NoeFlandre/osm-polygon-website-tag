from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.verification.shard_scan import (
    iter_bounded_batches,
    verify_optional_shard,
)


def _write(path: Path, rows: list[dict[str, object]]) -> Path:
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def test_verify_optional_shard_reports_unreadable_files(tmp_path: Path) -> None:
    path = tmp_path / "broken.parquet"
    path.write_bytes(b"not parquet")
    errors: list[str] = []

    verify_optional_shard(path, ("a",), "demo", lambda _p, _e: None, errors)

    assert len(errors) == 1
    assert errors[0].startswith(f"unreadable demo shard {path}: ")


def test_verify_optional_shard_skips_shards_missing_a_required_column(tmp_path: Path) -> None:
    path = _write(tmp_path / "legacy.parquet", [{"a": 1}])
    calls: list[Path] = []
    errors: list[str] = []

    verify_optional_shard(path, ("a", "b"), "demo", lambda p, _e: calls.append(p), errors)

    assert calls == []
    assert errors == []


def test_verify_optional_shard_forwards_and_wraps_failures(tmp_path: Path) -> None:
    path = _write(tmp_path / "shard.parquet", [{"a": 1, "b": 2}])
    calls: list[tuple[Path, list[str]]] = []
    errors: list[str] = []

    verify_optional_shard(path, ("a", "b"), "demo", lambda p, e: calls.append((p, e)), errors)

    assert calls == [(path, errors)]

    def explode(_path: Path, _errors: list[str]) -> None:
        raise RuntimeError("boom")

    verify_optional_shard(path, ("a",), "demo", explode, errors)

    assert errors == [f"demo invariant verification failed for {path}: boom"]


def test_iter_bounded_batches_numbers_bounded_projected_batches(tmp_path: Path) -> None:
    path = _write(tmp_path / "shard.parquet", [{"a": index, "b": -index} for index in range(5)])

    batches = list(iter_bounded_batches(path, ["a"], 2))

    assert [number for number, _batch in batches] == [0, 1, 2]
    assert [batch.num_rows for _number, batch in batches] == [2, 2, 1]
    assert all(batch.schema.names == ["a"] for _number, batch in batches)
