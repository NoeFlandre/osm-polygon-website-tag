"""Bounded atomic migration of enriched public polygon Parquets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    SCHEMA_VERSION,
    schema_matches,
)
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink

DEFAULT_BATCH_ROWS = 8_192


@dataclass(frozen=True)
class PublicSchemaMigrationResult:
    """Outcome of migrating one public polygon shard."""

    shard_path: Path
    row_count: int
    changed: bool
    shard_sha256: str
    max_batch_rows: int


def migrate_public_shard(
    shard_path: Path | str,
    *,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> PublicSchemaMigrationResult:
    """Project a v1.2 shard to v1.3 without PBF or network access."""
    shard = Path(shard_path)
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    row_count = parquet.metadata.num_rows
    if schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA):
        return _unchanged_migration_result(shard, row_count)
    if not schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA_V1_2):
        raise ValueError(f"unsupported polygon schema for migration: {shard.name}")

    staged = shard.with_name(f".{shard.name}.migrating")
    staged.unlink(missing_ok=True)
    max_pending_rows = _migrate_to_staged(
        parquet, staged, shard, row_count=row_count, batch_rows=batch_rows
    )
    return PublicSchemaMigrationResult(
        shard_path=shard,
        row_count=row_count,
        changed=True,
        shard_sha256=hash_shard(shard),
        max_batch_rows=max_pending_rows,
    )


def _unchanged_migration_result(shard: Path, row_count: int) -> PublicSchemaMigrationResult:
    """Return the no-op result for an already-current public shard."""
    return PublicSchemaMigrationResult(
        shard_path=shard,
        row_count=row_count,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=0,
    )


def _migrate_to_staged(
    parquet: pq.ParquetFile,
    staged: Path,
    shard: Path,
    *,
    row_count: int,
    batch_rows: int,
) -> int:
    """Write, validate, and atomically promote a migrated shard."""
    sink = BatchParquetSink(staged, POLYGON_PUBLIC_SCHEMA, batch_rows=batch_rows)
    try:
        _write_migration_batches(parquet, sink, batch_rows=batch_rows)
        sink.close()
        _validate_migrated_shard(staged, sink.row_count, row_count)
        atomic_promote_bundle([(staged, shard)])
        return sink.max_pending_rows
    except BaseException:
        sink.close()
        staged.unlink(missing_ok=True)
        raise


def _write_migration_batches(
    parquet: pq.ParquetFile, sink: BatchParquetSink, *, batch_rows: int
) -> None:
    """Project every bounded Arrow batch into the current schema."""
    for batch in parquet.iter_batches(batch_size=batch_rows):
        _write_migration_batch(batch, sink)


def _write_migration_batch(batch: pa.RecordBatch, sink: BatchParquetSink) -> None:
    """Project one Arrow batch without retaining source-sized state."""
    for original in batch.to_pylist():
        row = {name: original[name] for name in POLYGON_PUBLIC_SCHEMA.names}
        row["schema_version"] = SCHEMA_VERSION
        sink.add(row)


def _validate_migrated_shard(staged: Path, actual_rows: int, expected_rows: int) -> None:
    """Validate migrated row count and schema before promotion."""
    if actual_rows != expected_rows:
        raise ValueError("public schema migration changed row count")
    if not schema_matches(pq.read_schema(staged), POLYGON_PUBLIC_SCHEMA):
        raise ValueError("migrated public shard schema mismatch")


__all__ = ["PublicSchemaMigrationResult", "migrate_public_shard"]
