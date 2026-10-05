"""Parquet metadata helpers shared across layers."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq


def parquet_row_count(path: Path) -> int:
    """Return the row count recorded in a Parquet file footer."""
    return int(pq.ParquetFile(path).metadata.num_rows)
