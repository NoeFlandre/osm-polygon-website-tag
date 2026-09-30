"""Validation of language-detection fields in public polygon shards."""

from __future__ import annotations

import math
from collections.abc import Collection
from pathlib import Path

import pyarrow as pa

from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_COLUMN_NAMES
from osm_polygon_website_tag.reporting.verification.shard_scan import (
    iter_bounded_batches,
    verify_optional_shard,
)

_LANGUAGE_BATCH_ROWS = 512
_LANGUAGE_COLUMNS = (
    "website_text_status",
    "website_language",
    "website_language_probability",
    "contact_website_text_status",
    "contact_website_language",
    "contact_website_language_probability",
)


def verify_language_invariants(root: Path, errors: list[str]) -> None:
    """Verify nullable language pairs independently for every public shard."""
    verify_language_paths(sorted((root / "polygons").glob("*.parquet")), errors)


def verify_language_paths(paths: Collection[Path], errors: list[str]) -> None:
    """Verify nullable language pairs for an exact reducer input selection."""
    for path in sorted(paths):
        _verify_language_file(path, errors)


def _verify_language_file(path: Path, errors: list[str]) -> None:
    """Read and verify one language-bearing shard, reporting corrupt files."""
    verify_optional_shard(path, LANGUAGE_COLUMN_NAMES, "language", _verify_language_shard, errors)


def _verify_language_shard(path: Path, errors: list[str]) -> None:
    """Verify bounded batches from one shard that includes language fields."""
    for batch_number, batch in iter_bounded_batches(path, _LANGUAGE_COLUMNS, _LANGUAGE_BATCH_ROWS):
        _verify_language_batch(path, batch_number, batch, errors)


def _verify_language_batch(
    path: Path, batch_number: int, batch: pa.RecordBatch, errors: list[str]
) -> None:
    """Verify each row in one bounded Arrow batch."""
    for row_number in range(batch.num_rows):
        values = [batch.column(name)[row_number].as_py() for name in _LANGUAGE_COLUMNS]
        absolute_row = batch_number * _LANGUAGE_BATCH_ROWS + row_number
        _verify_language_row(path, absolute_row, values, errors)


def _verify_language_row(
    path: Path, row_number: int, values: list[object], errors: list[str]
) -> None:
    """Verify website and contact language pairs from one row."""
    row = dict(zip(_LANGUAGE_COLUMNS, values, strict=True))
    for prefix in ("website", "contact_website"):
        _verify_language_pair(
            path,
            row_number,
            prefix,
            row[f"{prefix}_text_status"],
            row[f"{prefix}_language"],
            row[f"{prefix}_language_probability"],
            errors,
        )


def _verify_language_pair(
    path: Path,
    row_number: int,
    prefix: str,
    status: object,
    label: object,
    probability: object,
    errors: list[str],
) -> None:
    """Verify one text status and its nullable language pair."""
    location = f"{path} row {row_number} {prefix}"
    if status == "success":
        _verify_successful_language_pair(location, label, probability, errors)
    elif _has_language_values(label, probability):
        errors.append(f"{location} language fields must be null when text is not successful")


def _verify_successful_language_pair(
    location: str, label: object, probability: object, errors: list[str]
) -> None:
    """Verify a successful text result has a complete language pair."""
    if not isinstance(label, str) or not label.strip():
        errors.append(f"{location} language label is missing")
    if not _valid_probability(probability):
        errors.append(f"{location} language probability is invalid")


def _has_language_values(label: object, probability: object) -> bool:
    """Return whether a non-successful row has a populated language field."""
    return label is not None or probability is not None


def _valid_probability(value: object) -> bool:
    """Return whether a language probability is a finite value in [0, 1]."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


__all__ = ["verify_language_invariants", "verify_language_paths"]
