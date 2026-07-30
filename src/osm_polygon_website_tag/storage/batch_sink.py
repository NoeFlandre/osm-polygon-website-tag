"""Bounded Parquet row sink."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


class BatchParquetSink:
    """Write dictionaries to Parquet while retaining at most ``batch_rows``."""

    def __init__(self, path: Path, schema: pa.Schema, *, batch_rows: int) -> None:
        if batch_rows < 1:
            raise ValueError("batch_rows must be positive")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.schema = schema
        self.batch_rows = batch_rows
        self._pending: list[dict[str, object]] = []
        self._writer = pq.ParquetWriter(path, schema, compression="snappy")
        self.row_count = 0
        self.max_pending_rows = 0
        self._closed = False

    def add(self, row: dict[str, object]) -> None:
        if self._closed:
            raise RuntimeError("sink is closed")
        self._pending.append(row)
        self.max_pending_rows = max(self.max_pending_rows, len(self._pending))
        if len(self._pending) >= self.batch_rows:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        self._writer.write_table(pa.Table.from_pylist(self._pending, schema=self.schema))
        self.row_count += len(self._pending)
        self._pending.clear()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._writer.close()
        self._closed = True

    def __enter__(self) -> BatchParquetSink:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
