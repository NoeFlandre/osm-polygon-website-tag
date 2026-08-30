"""Durable, source-bound checkpoint storage for polygon enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    SCHEMA_VERSION,
)
from osm_polygon_website_tag.pipeline import checkpoint_storage as _checkpoint_storage
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink

CHECKPOINT_VERSION = 1
CHECKPOINT_DIRECTORY_SUFFIX = ".enriching.parts"
CHECKPOINT_METADATA_NAME = _checkpoint_storage.CHECKPOINT_METADATA_NAME


@dataclass(frozen=True)
class EnrichmentCheckpoint:
    """Durable prefix of one shard's enriched rows."""

    directory: Path
    parts: tuple[Path, ...]
    completed_rows: int


def _checkpoint_directory(shard: Path) -> Path:
    return _checkpoint_storage.checkpoint_directory(shard, CHECKPOINT_DIRECTORY_SUFFIX)


def _checkpoint_part_path(directory: Path, index: int) -> Path:
    return _checkpoint_storage.checkpoint_part_path(directory, index)


def _write_checkpoint_metadata(
    directory: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
    schema_version: str = SCHEMA_VERSION,
) -> None:
    _checkpoint_storage.write_checkpoint_metadata(
        directory,
        {
            "checkpoint_version": CHECKPOINT_VERSION,
            "schema_version": schema_version,
            "source_row_count": source_row_count,
            "source_shard_sha256": source_shard_sha256,
        },
    )


def checkpoint_parts(
    directory: Path,
    *,
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
) -> tuple[Path, ...]:
    """Validate and return sequential durable checkpoint parts."""
    return _checkpoint_storage.validate_checkpoint_parts(
        directory,
        schema=schema,
        label="enrichment",
    )


def load_checkpoint(
    shard: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
    schema_version: str = SCHEMA_VERSION,
) -> EnrichmentCheckpoint:
    """Load a source-bound checkpoint or create its empty durable directory."""
    directory = _checkpoint_directory(shard)
    directory.mkdir(parents=True, exist_ok=True)
    metadata_path = directory / CHECKPOINT_METADATA_NAME
    _cleanup_checkpoint_temps(directory, metadata_path)
    _ensure_checkpoint_metadata(
        directory,
        metadata_path,
        shard=shard,
        source_row_count=source_row_count,
        source_shard_sha256=source_shard_sha256,
        schema_version=schema_version,
    )
    parts = checkpoint_parts(directory, schema=schema)
    _validate_checkpoint_contents(directory, parts)
    completed_rows = sum(pq.ParquetFile(part).metadata.num_rows for part in parts)
    if completed_rows > source_row_count:
        raise ValueError(f"enrichment checkpoint exceeds source row count: {shard.name}")
    return EnrichmentCheckpoint(directory, parts, completed_rows)


def _cleanup_checkpoint_temps(directory: Path, metadata_path: Path) -> None:
    """Remove only known temporary checkpoint files before loading."""
    _checkpoint_storage.cleanup_checkpoint_temps(directory, metadata_path)


def _ensure_checkpoint_metadata(
    directory: Path,
    metadata_path: Path,
    *,
    shard: Path,
    source_row_count: int,
    source_shard_sha256: str,
    schema_version: str = SCHEMA_VERSION,
) -> None:
    """Validate existing source identity or create a fresh metadata file."""
    expected = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "schema_version": schema_version,
        "source_row_count": source_row_count,
        "source_shard_sha256": source_shard_sha256,
    }
    _checkpoint_storage.ensure_checkpoint_metadata(
        directory,
        metadata_path,
        shard=shard,
        expected=expected,
        label="enrichment",
        mismatch_description="source shard",
    )


def _validate_checkpoint_contents(directory: Path, parts: tuple[Path, ...]) -> None:
    """Reject unknown scratch files from a source-bound checkpoint."""
    _checkpoint_storage.validate_checkpoint_contents(directory, parts, label="enrichment")


def write_checkpoint_part(
    directory: Path,
    index: int,
    rows: list[dict[str, object]],
    *,
    batch_rows: int,
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
) -> None:
    """Write one completed enrichment batch and publish it atomically."""
    _checkpoint_storage.write_checkpoint_part(
        directory,
        index,
        rows,
        batch_rows=batch_rows,
        schema=schema,
        label="enrichment",
    )


def _write_checkpoint_rows(sink: BatchParquetSink, rows: list[dict[str, object]]) -> None:
    """Stream one completed enrichment batch into its Parquet sink."""
    _checkpoint_storage.write_checkpoint_rows(sink, rows)


def _validate_checkpoint_part(
    path: Path,
    actual_rows: int,
    expected_rows: int,
    *,
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
) -> None:
    """Validate one durable checkpoint part before promotion."""
    _checkpoint_storage.validate_checkpoint_part(
        path,
        actual_rows,
        expected_rows,
        schema=schema,
        label="enrichment",
    )


def assemble_checkpoint(
    parts: tuple[Path, ...],
    staged: Path,
    *,
    batch_rows: int,
    row_count: int,
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
) -> int:
    """Stream durable parts into one final staged Parquet.

    Checkpoint parts already contain Arrow record batches in the target
    schema. Writing those batches directly avoids converting every row to a
    Python dictionary and back during final assembly while retaining the
    existing bounded row-group size and deterministic part order.
    """
    return _checkpoint_storage.assemble_checkpoint(
        parts,
        staged,
        batch_rows=batch_rows,
        row_count=row_count,
        schema=schema,
        label="enrichment",
    )


def _write_checkpoint_parts(
    writer: pq.ParquetWriter, parts: tuple[Path, ...], *, batch_rows: int
) -> tuple[int, int]:
    """Stream all durable checkpoint parts into the final writer."""
    return _checkpoint_storage.write_checkpoint_parts(writer, parts, batch_rows=batch_rows)


def _validate_assembled_checkpoint(
    staged: Path,
    actual_rows: int,
    expected_rows: int,
    *,
    schema: pa.Schema,
) -> None:
    """Validate final row count and schema before shard promotion."""
    _checkpoint_storage.validate_assembled_checkpoint(
        staged,
        actual_rows,
        expected_rows,
        schema=schema,
        label="enrichment",
    )


__all__ = [
    "EnrichmentCheckpoint",
    "assemble_checkpoint",
    "checkpoint_parts",
    "load_checkpoint",
    "write_checkpoint_part",
]
