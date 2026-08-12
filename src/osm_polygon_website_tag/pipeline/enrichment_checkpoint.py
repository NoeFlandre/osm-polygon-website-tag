"""Durable, source-bound checkpoint storage for polygon enrichment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    SCHEMA_VERSION,
    schema_matches,
)
from osm_polygon_website_tag.runtime.run_state import atomic_write_json
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink

CHECKPOINT_VERSION = 1
CHECKPOINT_DIRECTORY_SUFFIX = ".enriching.parts"
CHECKPOINT_METADATA_NAME = "checkpoint.json"


@dataclass(frozen=True)
class EnrichmentCheckpoint:
    """Durable prefix of one shard's enriched rows."""

    directory: Path
    parts: tuple[Path, ...]
    completed_rows: int


def _checkpoint_directory(shard: Path) -> Path:
    return shard.with_name(f".{shard.name}{CHECKPOINT_DIRECTORY_SUFFIX}")


def _checkpoint_part_path(directory: Path, index: int) -> Path:
    return directory / f"part-{index:08d}.parquet"


def _write_checkpoint_metadata(
    directory: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
) -> None:
    atomic_write_json(
        directory / CHECKPOINT_METADATA_NAME,
        {
            "checkpoint_version": CHECKPOINT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source_row_count": source_row_count,
            "source_shard_sha256": source_shard_sha256,
        },
    )


def checkpoint_parts(directory: Path) -> tuple[Path, ...]:
    """Validate and return sequential durable checkpoint parts."""
    parts = sorted(directory.glob("part-*.parquet"), key=lambda path: path.name)
    for index, part in enumerate(parts):
        if part.name != _checkpoint_part_path(directory, index).name:
            raise ValueError(f"non-sequential enrichment checkpoint part: {part.name}")
        parquet = pq.ParquetFile(part)
        if not schema_matches(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA):
            raise ValueError(f"invalid enrichment checkpoint schema: {part.name}")
        if parquet.metadata.num_rows < 1:
            raise ValueError(f"empty enrichment checkpoint part: {part.name}")
    return tuple(parts)


def load_checkpoint(
    shard: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
) -> EnrichmentCheckpoint:
    """Load a source-bound checkpoint or create its empty durable directory."""
    directory = _checkpoint_directory(shard)
    directory.mkdir(parents=True, exist_ok=True)
    for temporary in directory.glob(".*.writing"):
        temporary.unlink(missing_ok=True)
    metadata_path = directory / CHECKPOINT_METADATA_NAME
    metadata_path.with_suffix(metadata_path.suffix + ".tmp").unlink(missing_ok=True)
    if metadata_path.exists():
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source_row_count": source_row_count,
            "source_shard_sha256": source_shard_sha256,
        }
        if payload != expected:
            raise ValueError(f"enrichment checkpoint does not match source shard: {shard.name}")
    else:
        if any(directory.iterdir()):
            raise ValueError(f"unrecognized enrichment checkpoint contents: {directory}")
        _write_checkpoint_metadata(
            directory,
            source_row_count=source_row_count,
            source_shard_sha256=source_shard_sha256,
        )
    parts = checkpoint_parts(directory)
    allowed = {CHECKPOINT_METADATA_NAME, *(part.name for part in parts)}
    unknown = sorted(child.name for child in directory.iterdir() if child.name not in allowed)
    if unknown:
        raise ValueError(f"unrecognized enrichment checkpoint contents: {unknown}")
    completed_rows = sum(pq.ParquetFile(part).metadata.num_rows for part in parts)
    if completed_rows > source_row_count:
        raise ValueError(f"enrichment checkpoint exceeds source row count: {shard.name}")
    return EnrichmentCheckpoint(directory, parts, completed_rows)


def write_checkpoint_part(
    directory: Path,
    index: int,
    rows: list[dict[str, object]],
    *,
    batch_rows: int,
) -> None:
    """Write one completed enrichment batch and publish it atomically."""
    if not rows:
        return
    target = _checkpoint_part_path(directory, index)
    if target.exists():
        raise ValueError(f"enrichment checkpoint part already exists: {target.name}")
    temporary = directory / f".{target.name}.writing"
    sink = BatchParquetSink(temporary, POLYGON_PUBLIC_SCHEMA, batch_rows=batch_rows)
    try:
        for row in rows:
            sink.add(row)
        sink.close()
        if sink.row_count != len(rows):
            raise ValueError("enrichment checkpoint row count changed")
        if not schema_matches(pq.read_schema(temporary), POLYGON_PUBLIC_SCHEMA):
            raise ValueError("enrichment checkpoint schema mismatch")
        atomic_promote_bundle([(temporary, target)])
    finally:
        sink.close()
        temporary.unlink(missing_ok=True)


def assemble_checkpoint(
    parts: tuple[Path, ...],
    staged: Path,
    *,
    batch_rows: int,
    row_count: int,
) -> int:
    """Stream durable parts into one final staged Parquet.

    Checkpoint parts already contain Arrow record batches in the target
    schema. Writing those batches directly avoids converting every row to a
    Python dictionary and back during final assembly while retaining the
    existing bounded row-group size and deterministic part order.
    """
    staged.unlink(missing_ok=True)
    assembled_rows = 0
    max_batch_rows = 0
    try:
        with pq.ParquetWriter(staged, POLYGON_PUBLIC_SCHEMA, compression="snappy") as writer:
            for part in parts:
                parquet = pq.ParquetFile(part)
                for batch in parquet.iter_batches(batch_size=batch_rows):
                    writer.write_batch(batch)
                    assembled_rows += batch.num_rows
                    max_batch_rows = max(max_batch_rows, batch.num_rows)
        if assembled_rows != row_count:
            raise ValueError("enrichment row count changed while assembling checkpoint")
        if not schema_matches(pq.read_schema(staged), POLYGON_PUBLIC_SCHEMA):
            raise ValueError("assembled enrichment schema mismatch")
        return max_batch_rows
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


__all__ = [
    "EnrichmentCheckpoint",
    "assemble_checkpoint",
    "checkpoint_parts",
    "load_checkpoint",
    "write_checkpoint_part",
]
