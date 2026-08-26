"""Durable, source/model-bound checkpoint storage for language detection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_SCHEMA_VERSION
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_4,
    schema_matches,
)
from osm_polygon_website_tag.pipeline.glotlid import ModelIdentity
from osm_polygon_website_tag.runtime.run_state import atomic_write_json
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink

CHECKPOINT_VERSION = 1
CHECKPOINT_DIRECTORY_SUFFIX = ".language.parts"
CHECKPOINT_METADATA_NAME = "checkpoint.json"


@dataclass(frozen=True)
class LanguageCheckpoint:
    """Durable prefix of one language-detection shard."""

    directory: Path
    parts: tuple[Path, ...]
    completed_rows: int


def _checkpoint_directory(shard: Path) -> Path:
    return shard.with_name(f".{shard.name}{CHECKPOINT_DIRECTORY_SUFFIX}")


def _checkpoint_part_path(directory: Path, index: int) -> Path:
    return directory / f"part-{index:08d}.parquet"


def _metadata(
    *,
    source_row_count: int,
    source_shard_sha256: str,
    model: ModelIdentity,
) -> dict[str, object]:
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "schema_version": LANGUAGE_SCHEMA_VERSION,
        "source_row_count": source_row_count,
        "source_shard_sha256": source_shard_sha256,
        "model_repository": model.repository,
        "model_filename": model.filename,
        "model_revision": model.revision,
        "model_sha256": model.sha256,
    }


def _write_checkpoint_metadata(
    directory: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
    model: ModelIdentity,
) -> None:
    atomic_write_json(
        directory / CHECKPOINT_METADATA_NAME,
        _metadata(
            source_row_count=source_row_count,
            source_shard_sha256=source_shard_sha256,
            model=model,
        ),
    )


def checkpoint_parts(directory: Path) -> tuple[Path, ...]:
    """Validate and return sequential durable language checkpoint parts."""
    parts = sorted(directory.glob("part-*.parquet"), key=lambda path: path.name)
    for index, part in enumerate(parts):
        if part.name != _checkpoint_part_path(directory, index).name:
            raise ValueError(f"non-sequential language checkpoint part: {part.name}")
        parquet = pq.ParquetFile(part)
        if not schema_matches(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_4):
            raise ValueError(f"invalid language checkpoint schema: {part.name}")
        if parquet.metadata.num_rows < 1:
            raise ValueError(f"empty language checkpoint part: {part.name}")
    return tuple(parts)


def load_language_checkpoint(
    shard: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
    model: ModelIdentity,
) -> LanguageCheckpoint:
    """Load a source/model-bound checkpoint or create its empty directory."""
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
        model=model,
    )
    parts = checkpoint_parts(directory)
    _validate_checkpoint_contents(directory, parts)
    completed_rows = sum(pq.ParquetFile(part).metadata.num_rows for part in parts)
    if completed_rows > source_row_count:
        raise ValueError(f"language checkpoint exceeds source row count: {shard.name}")
    return LanguageCheckpoint(directory, parts, completed_rows)


def _cleanup_checkpoint_temps(directory: Path, metadata_path: Path) -> None:
    """Remove only known temporary language checkpoint files."""
    for temporary in directory.glob(".*.writing"):
        temporary.unlink(missing_ok=True)
    metadata_path.with_suffix(metadata_path.suffix + ".tmp").unlink(missing_ok=True)


def _ensure_checkpoint_metadata(
    directory: Path,
    metadata_path: Path,
    *,
    shard: Path,
    source_row_count: int,
    source_shard_sha256: str,
    model: ModelIdentity,
) -> None:
    """Validate existing source/model identity or create fresh metadata."""
    expected = _metadata(
        source_row_count=source_row_count,
        source_shard_sha256=source_shard_sha256,
        model=model,
    )
    if metadata_path.exists():
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if payload != expected:
            raise ValueError(
                f"language checkpoint does not match source or model identity: {shard.name}"
            )
        return
    if any(directory.iterdir()):
        raise ValueError(f"unrecognized language checkpoint contents: {directory}")
    atomic_write_json(metadata_path, expected)


def _validate_checkpoint_contents(directory: Path, parts: tuple[Path, ...]) -> None:
    """Reject unknown scratch files from a source-bound checkpoint."""
    allowed = {CHECKPOINT_METADATA_NAME, *(part.name for part in parts)}
    unknown = sorted(child.name for child in directory.iterdir() if child.name not in allowed)
    if unknown:
        raise ValueError(f"unrecognized language checkpoint contents: {unknown}")


def write_language_checkpoint_part(
    directory: Path,
    index: int,
    rows: list[dict[str, object]],
    *,
    batch_rows: int,
) -> None:
    """Write one completed language batch and publish it atomically."""
    if not rows:
        return
    target = _checkpoint_part_path(directory, index)
    if target.exists():
        raise ValueError(f"language checkpoint part already exists: {target.name}")
    temporary = directory / f".{target.name}.writing"
    sink = BatchParquetSink(temporary, POLYGON_PUBLIC_SCHEMA_V1_4, batch_rows=batch_rows)
    try:
        for row in rows:
            sink.add(row)
        sink.close()
        _validate_checkpoint_part(temporary, sink.row_count, len(rows))
        atomic_promote_bundle([(temporary, target)])
    finally:
        sink.close()
        temporary.unlink(missing_ok=True)


def _validate_checkpoint_part(path: Path, actual_rows: int, expected_rows: int) -> None:
    """Validate one durable language checkpoint part before promotion."""
    if actual_rows != expected_rows:
        raise ValueError("language checkpoint row count changed")
    if not schema_matches(pq.read_schema(path), POLYGON_PUBLIC_SCHEMA_V1_4):
        raise ValueError("language checkpoint schema mismatch")


def assemble_language_checkpoint(
    parts: tuple[Path, ...],
    staged: Path,
    *,
    batch_rows: int,
    row_count: int,
) -> int:
    """Stream durable language parts into one validated staged Parquet."""
    staged.unlink(missing_ok=True)
    assembled_rows = 0
    max_batch_rows = 0
    try:
        with pq.ParquetWriter(staged, POLYGON_PUBLIC_SCHEMA_V1_4, compression="snappy") as writer:
            for part in parts:
                parquet = pq.ParquetFile(part)
                for batch in parquet.iter_batches(batch_size=batch_rows):
                    writer.write_batch(batch)
                    assembled_rows += batch.num_rows
                    max_batch_rows = max(max_batch_rows, batch.num_rows)
        _validate_assembled_checkpoint(staged, assembled_rows, row_count)
        return max_batch_rows
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _validate_assembled_checkpoint(staged: Path, actual_rows: int, expected_rows: int) -> None:
    """Validate final language row count and schema before promotion."""
    if actual_rows != expected_rows:
        raise ValueError("language row count changed while assembling checkpoint")
    if not schema_matches(pq.read_schema(staged), POLYGON_PUBLIC_SCHEMA_V1_4):
        raise ValueError("assembled language schema mismatch")


__all__ = [
    "LanguageCheckpoint",
    "assemble_language_checkpoint",
    "checkpoint_parts",
    "load_language_checkpoint",
    "write_language_checkpoint_part",
]
