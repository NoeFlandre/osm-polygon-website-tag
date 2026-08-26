"""Validation of language-detection fields in public polygon shards."""

from __future__ import annotations

import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_COLUMN_NAMES

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
    for path in sorted((root / "polygons").glob("*.parquet")):
        _verify_language_file(path, errors)


def _verify_language_file(path: Path, errors: list[str]) -> None:
    """Read and verify one language-bearing shard, reporting corrupt files."""
    try:
        schema = pq.read_schema(path)
    except Exception as exc:
        errors.append(f"unreadable language shard {path}: {exc}")
        return
    if not all(name in schema.names for name in LANGUAGE_COLUMN_NAMES):
        return
    try:
        _verify_language_shard(path, errors)
    except Exception as exc:
        errors.append(f"language invariant verification failed for {path}: {exc}")


def _verify_language_shard(path: Path, errors: list[str]) -> None:
    """Verify bounded batches from one shard that includes language fields."""
    parquet = pq.ParquetFile(path)
    for batch_number, batch in enumerate(
        parquet.iter_batches(batch_size=_LANGUAGE_BATCH_ROWS, columns=_LANGUAGE_COLUMNS)
    ):
        _verify_language_batch(path, batch_number, batch, errors)


def _verify_language_batch(
    path: Path, batch_number: int, batch: pa.RecordBatch, errors: list[str]
) -> None:
    """Verify each row in one bounded Arrow batch."""
    for row_number in range(batch.num_rows):
        values = [batch.column(index)[row_number].as_py() for index in range(6)]
        absolute_row = batch_number * _LANGUAGE_BATCH_ROWS + row_number
        _verify_language_row(path, absolute_row, values, errors)


def _verify_language_row(
    path: Path, row_number: int, values: list[object], errors: list[str]
) -> None:
    """Verify website and contact language pairs from one row."""
    _verify_language_pair(path, row_number, "website", values[0], values[1], values[2], errors)
    _verify_language_pair(
        path, row_number, "contact_website", values[3], values[4], values[5], errors
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


__all__ = ["verify_language_invariants"]
