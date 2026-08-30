"""Shared bounded Parquet checkpoint mechanics for resumable pipeline stages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.runtime.run_state import atomic_write_json
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink

CHECKPOINT_METADATA_NAME = "checkpoint.json"


class _RowSink(Protocol):
    def add(self, row: dict[str, object]) -> None: ...


def checkpoint_directory(shard: Path, suffix: str) -> Path:
    """Return the source-scoped directory used for durable checkpoint parts."""
    return shard.with_name(f".{shard.name}{suffix}")


def checkpoint_part_path(directory: Path, index: int) -> Path:
    """Return the stable zero-padded path for one checkpoint part."""
    return directory / f"part-{index:08d}.parquet"


def cleanup_checkpoint_temps(directory: Path, metadata_path: Path) -> None:
    """Remove only known temporary checkpoint files before loading."""
    for temporary in directory.glob(".*.writing"):
        temporary.unlink(missing_ok=True)
    metadata_path.with_suffix(metadata_path.suffix + ".tmp").unlink(missing_ok=True)


def ensure_checkpoint_metadata(
    directory: Path,
    metadata_path: Path,
    *,
    shard: Path,
    expected: Mapping[str, object],
    label: str,
    mismatch_description: str,
) -> None:
    """Validate existing metadata or create the source-bound contract."""
    if metadata_path.exists():
        payload = json.loads(metadata_path.read_bytes())
        if payload != expected:
            raise ValueError(
                f"{label} checkpoint does not match {mismatch_description}: {shard.name}"
            )
        return
    if any(directory.iterdir()):
        raise ValueError(f"unrecognized {label} checkpoint contents: {directory}")
    write_checkpoint_metadata(directory, dict(expected))


def validate_checkpoint_parts(
    directory: Path,
    *,
    schema: pa.Schema,
    label: str,
) -> tuple[Path, ...]:
    """Validate sequential, non-empty checkpoint parts for one stage."""
    parts = sorted(directory.glob("part-*.parquet"))
    for index, part in enumerate(parts):
        if part.name != checkpoint_part_path(directory, index).name:
            raise ValueError(f"non-sequential {label} checkpoint part: {part.name}")
        parquet = pq.ParquetFile(part)
        if not parquet.schema_arrow.equals(schema, check_metadata=True):
            raise ValueError(f"invalid {label} checkpoint schema: {part.name}")
        if parquet.metadata.num_rows < 1:
            raise ValueError(f"empty {label} checkpoint part: {part.name}")
    return tuple(parts)


def validate_checkpoint_contents(
    directory: Path,
    parts: tuple[Path, ...],
    *,
    label: str,
) -> None:
    """Reject unknown files from a source-bound checkpoint directory."""
    allowed = {CHECKPOINT_METADATA_NAME, *(part.name for part in parts)}
    unknown = sorted(child.name for child in directory.iterdir() if child.name not in allowed)
    if unknown:
        raise ValueError(f"unrecognized {label} checkpoint contents: {unknown}")


def write_checkpoint_rows(sink: _RowSink, rows: list[dict[str, object]]) -> None:
    """Write one completed checkpoint batch into a bounded row sink."""
    for row in rows:
        sink.add(row)


def validate_checkpoint_part(
    path: Path,
    actual_rows: int,
    expected_rows: int,
    *,
    schema: pa.Schema,
    label: str,
) -> None:
    """Validate one durable checkpoint part before promotion."""
    if actual_rows != expected_rows:
        raise ValueError(f"{label} checkpoint row count changed")
    if not pq.read_schema(path).equals(schema, check_metadata=True):
        raise ValueError(f"{label} checkpoint schema mismatch")


def write_checkpoint_part(
    directory: Path,
    index: int,
    rows: list[dict[str, object]],
    *,
    batch_rows: int,
    schema: pa.Schema,
    label: str,
) -> None:
    """Write one checkpoint part and promote it atomically."""
    if not rows:
        return
    target = checkpoint_part_path(directory, index)
    if target.exists():
        raise ValueError(f"{label} checkpoint part already exists: {target.name}")
    temporary = directory / f".{target.name}.writing"
    sink = BatchParquetSink(temporary, schema, batch_rows=batch_rows)
    try:
        write_checkpoint_rows(sink, rows)
        sink.close()
        validate_checkpoint_part(
            temporary,
            sink.row_count,
            len(rows),
            schema=schema,
            label=label,
        )
        atomic_promote_bundle([(temporary, target)])
    finally:
        sink.close()
        temporary.unlink(missing_ok=True)


def write_checkpoint_parts(
    writer: pq.ParquetWriter,
    parts: tuple[Path, ...],
    *,
    batch_rows: int,
) -> tuple[int, int]:
    """Stream all durable parts into a writer and return row-size metrics."""
    assembled_rows = 0
    max_batch_rows = 0
    for part in parts:
        parquet = pq.ParquetFile(part)
        for batch in parquet.iter_batches(batch_size=batch_rows):
            writer.write_batch(batch)
            assembled_rows += batch.num_rows
            max_batch_rows = max(max_batch_rows, batch.num_rows)
    return assembled_rows, max_batch_rows


def assemble_checkpoint(
    parts: tuple[Path, ...],
    staged: Path,
    *,
    batch_rows: int,
    row_count: int,
    schema: pa.Schema,
    label: str,
) -> int:
    """Assemble durable parts into a validated staged Parquet file."""
    staged.unlink(missing_ok=True)
    try:
        with pq.ParquetWriter(staged, schema, compression="snappy") as writer:
            assembled_rows, max_batch_rows = write_checkpoint_parts(
                writer, parts, batch_rows=batch_rows
            )
        validate_assembled_checkpoint(
            staged,
            assembled_rows,
            row_count,
            schema=schema,
            label=label,
        )
        return max_batch_rows
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def validate_assembled_checkpoint(
    staged: Path,
    actual_rows: int,
    expected_rows: int,
    *,
    schema: pa.Schema,
    label: str,
) -> None:
    """Validate final row count and schema before shard promotion."""
    if actual_rows != expected_rows:
        raise ValueError(f"{label} row count changed while assembling checkpoint")
    if not pq.read_schema(staged).equals(schema, check_metadata=True):
        raise ValueError(f"assembled {label} schema mismatch")


def write_checkpoint_metadata(directory: Path, payload: dict[str, object]) -> None:
    """Persist checkpoint metadata through the shared atomic JSON boundary."""
    atomic_write_json(directory / CHECKPOINT_METADATA_NAME, payload)


__all__ = [
    "CHECKPOINT_METADATA_NAME",
    "assemble_checkpoint",
    "checkpoint_directory",
    "checkpoint_part_path",
    "cleanup_checkpoint_temps",
    "ensure_checkpoint_metadata",
    "validate_assembled_checkpoint",
    "validate_checkpoint_contents",
    "validate_checkpoint_part",
    "validate_checkpoint_parts",
    "write_checkpoint_metadata",
    "write_checkpoint_part",
    "write_checkpoint_parts",
    "write_checkpoint_rows",
]
