"""Bounded status batches read for the resume summary."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.application import resume_planner
from osm_polygon_website_tag.application.resume_planner import _status_batches

_COLUMNS = list(resume_planner._STATUS_SUMMARY_COLUMNS.values())


def _table(rows: int, columns: list[str]) -> pa.Table:
    data: dict[str, list[object]] = {name: ["success"] * rows for name in columns}
    data["unrelated"] = list(range(rows))
    return pa.table(data)


def _write(tmp_path: Path, rows: int, columns: list[str]) -> Path:
    path = tmp_path / "shard.parquet"
    pq.write_table(_table(rows, columns), path)
    return path


def test_a_table_is_split_into_its_own_batches() -> None:
    table = _table(5, _COLUMNS)

    batches = _status_batches(table)

    assert batches is not None
    assert pa.Table.from_batches(list(batches)).equals(table)


def test_a_shard_yields_only_the_status_columns_in_bounded_batches(tmp_path: Path) -> None:
    path = _write(tmp_path, 20_000, _COLUMNS)

    batches = _status_batches(path)

    assert batches is not None
    sizes = [batch.num_rows for batch in batches]
    assert sizes[0] == 8_192
    assert sum(sizes) == 20_000
    first = next(iter(_status_batches(path) or []))
    assert first.schema.names == _COLUMNS


def test_a_shard_missing_a_status_column_has_no_batches(tmp_path: Path) -> None:
    assert _status_batches(_write(tmp_path, 3, _COLUMNS[:-1])) is None
