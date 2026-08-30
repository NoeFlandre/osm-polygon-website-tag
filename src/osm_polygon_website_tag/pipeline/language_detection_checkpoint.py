"""Durable, source/model-bound checkpoint storage for language detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_SCHEMA_VERSION
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.pipeline import checkpoint_storage as _checkpoint_storage
from osm_polygon_website_tag.pipeline.glotlid import ModelIdentity

CHECKPOINT_VERSION = 1
CHECKPOINT_DIRECTORY_SUFFIX = ".language.parts"
CHECKPOINT_METADATA_NAME = _checkpoint_storage.CHECKPOINT_METADATA_NAME


@dataclass(frozen=True)
class LanguageCheckpoint:
    """Durable prefix of one language-detection shard."""

    directory: Path
    parts: tuple[Path, ...]
    completed_rows: int


def _checkpoint_directory(shard: Path) -> Path:
    return _checkpoint_storage.checkpoint_directory(shard, CHECKPOINT_DIRECTORY_SUFFIX)


def _checkpoint_part_path(directory: Path, index: int) -> Path:
    return _checkpoint_storage.checkpoint_part_path(directory, index)


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


def checkpoint_parts(directory: Path) -> tuple[Path, ...]:
    """Validate and return sequential durable language checkpoint parts."""
    return _checkpoint_storage.validate_checkpoint_parts(
        directory,
        schema=POLYGON_PUBLIC_SCHEMA_V1_4,
        label="language",
    )


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
    _checkpoint_storage.cleanup_checkpoint_temps(directory, metadata_path)


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
    _checkpoint_storage.ensure_checkpoint_metadata(
        directory,
        metadata_path,
        shard=shard,
        expected=expected,
        label="language",
        mismatch_description="source or model identity",
    )


def _validate_checkpoint_contents(directory: Path, parts: tuple[Path, ...]) -> None:
    """Reject unknown scratch files from a source-bound checkpoint."""
    _checkpoint_storage.validate_checkpoint_contents(directory, parts, label="language")


def write_language_checkpoint_part(
    directory: Path,
    index: int,
    rows: list[dict[str, object]],
    *,
    batch_rows: int,
) -> None:
    """Write one completed language batch and publish it atomically."""
    _checkpoint_storage.write_checkpoint_part(
        directory,
        index,
        rows,
        batch_rows=batch_rows,
        schema=POLYGON_PUBLIC_SCHEMA_V1_4,
        label="language",
    )


def _validate_checkpoint_part(path: Path, actual_rows: int, expected_rows: int) -> None:
    """Validate one durable language checkpoint part before promotion."""
    _checkpoint_storage.validate_checkpoint_part(
        path,
        actual_rows,
        expected_rows,
        schema=POLYGON_PUBLIC_SCHEMA_V1_4,
        label="language",
    )


def assemble_language_checkpoint(
    parts: tuple[Path, ...],
    staged: Path,
    *,
    batch_rows: int,
    row_count: int,
) -> int:
    """Stream durable language parts into one validated staged Parquet."""
    return _checkpoint_storage.assemble_checkpoint(
        parts,
        staged,
        batch_rows=batch_rows,
        row_count=row_count,
        schema=POLYGON_PUBLIC_SCHEMA_V1_4,
        label="language",
    )


def _validate_assembled_checkpoint(staged: Path, actual_rows: int, expected_rows: int) -> None:
    """Validate final language row count and schema before promotion."""
    _checkpoint_storage.validate_assembled_checkpoint(
        staged,
        actual_rows,
        expected_rows,
        schema=POLYGON_PUBLIC_SCHEMA_V1_4,
        label="language",
    )


__all__ = [
    "LanguageCheckpoint",
    "assemble_language_checkpoint",
    "checkpoint_parts",
    "load_language_checkpoint",
    "write_language_checkpoint_part",
]
