"""Tests for the shared Parquet footer helpers."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.storage.parquet import parquet_row_count


def test_parquet_row_count_reads_the_footer(tmp_path: Path) -> None:
    path = tmp_path / "rows.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), path)
    empty = tmp_path / "empty.parquet"
    pq.write_table(pa.table({"value": pa.array([], type=pa.int64())}), empty)

    assert parquet_row_count(path) == 3
    assert parquet_row_count(empty) == 0
    assert type(parquet_row_count(path)) is int
