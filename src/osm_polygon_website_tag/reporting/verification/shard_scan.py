"""Shared read-and-scan scaffolding for optional-column shard verifiers."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def verify_optional_shard(
    path: Path,
    required_columns: Collection[str],
    label: str,
    verify_shard: Callable[[Path, list[str]], None],
    errors: list[str],
) -> None:
    """Verify one shard that carries ``required_columns``, reporting corrupt files.

    Shards without every required column predate the stage and are skipped.
    """
    try:
        schema = pq.read_schema(path)
    except Exception as exc:
        errors.append(f"unreadable {label} shard {path}: {exc}")
        return
    if not all(name in schema.names for name in required_columns):
        return
    try:
        verify_shard(path, errors)
    except Exception as exc:
        errors.append(f"{label} invariant verification failed for {path}: {exc}")


def iter_bounded_batches(
    path: Path, columns: Sequence[str], batch_rows: int
) -> Iterator[tuple[int, pa.RecordBatch]]:
    """Yield ``(batch_number, batch)`` for bounded batches of the named columns."""
    parquet = pq.ParquetFile(path)
    yield from enumerate(parquet.iter_batches(batch_size=batch_rows, columns=columns))


__all__ = ["iter_bounded_batches", "verify_optional_shard"]
