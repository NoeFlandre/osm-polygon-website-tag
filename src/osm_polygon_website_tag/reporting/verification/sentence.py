"""Validation of sentence-segmentation fields in public polygon shards.

Every v1.5 row must explain itself: a segmented row carries counted sentences,
and every other row records why it was skipped. The invariants below tie those
statuses back to the text status and the detected language, so a shard cannot
claim sentences for text that was never fetched, or claim an uncovered language
for one the segmenter actually supports.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.sentence_schema import (
    SENTENCE_ABSENT,
    SENTENCE_COLUMN_NAMES,
    SENTENCE_STATUSES,
    SENTENCE_SUCCESS,
    SENTENCE_UNSUPPORTED_LANGUAGE,
)
from osm_polygon_website_tag.pipeline.sentence_languages import sat_code_for_glotlid_label

_SENTENCE_BATCH_ROWS = 512
_TEXT_SUCCESS = "success"
_TEXT_ABSENT = "absent"
_PREFIXES = ("website", "contact_website")
# Written out rather than generated: the read order is what the batch column
# indices below rely on, and a comprehension would hide that coupling.
_SENTENCE_COLUMNS = (
    "website_text_status",
    "website_language",
    "website_sentences",
    "website_sentence_count",
    "website_sentence_status",
    "contact_website_text_status",
    "contact_website_language",
    "contact_website_sentences",
    "contact_website_sentence_count",
    "contact_website_sentence_status",
)
_FIELDS_PER_PREFIX = 5


def verify_sentence_invariants(root: Path, errors: list[str]) -> None:
    """Verify sentence fields independently for every public shard."""
    for path in sorted((root / "polygons").glob("*.parquet")):
        _verify_sentence_file(path, errors)


def _verify_sentence_file(path: Path, errors: list[str]) -> None:
    """Read and verify one sentence-bearing shard, reporting corrupt files."""
    try:
        schema = pq.read_schema(path)
    except Exception as exc:
        errors.append(f"unreadable sentence shard {path}: {exc}")
        return
    if not all(name in schema.names for name in SENTENCE_COLUMN_NAMES):
        return
    try:
        _verify_sentence_shard(path, errors)
    except Exception as exc:
        errors.append(f"sentence invariant verification failed for {path}: {exc}")


def _verify_sentence_shard(path: Path, errors: list[str]) -> None:
    """Verify bounded batches from one shard that includes sentence fields."""
    parquet = pq.ParquetFile(path)
    for batch_number, batch in enumerate(
        parquet.iter_batches(batch_size=_SENTENCE_BATCH_ROWS, columns=list(_SENTENCE_COLUMNS))
    ):
        _verify_sentence_batch(path, batch_number, batch, errors)


def _verify_sentence_batch(
    path: Path, batch_number: int, batch: pa.RecordBatch, errors: list[str]
) -> None:
    """Verify each row in one bounded Arrow batch."""
    for row_number in range(batch.num_rows):
        absolute_row = batch_number * _SENTENCE_BATCH_ROWS + row_number
        for index, prefix in enumerate(_PREFIXES):
            _verify_sentence_field(
                location=f"{path} row {absolute_row} {prefix}",
                text_status=batch.column(index * _FIELDS_PER_PREFIX)[row_number].as_py(),
                language=batch.column(index * _FIELDS_PER_PREFIX + 1)[row_number].as_py(),
                sentences=batch.column(index * _FIELDS_PER_PREFIX + 2)[row_number].as_py(),
                count=batch.column(index * _FIELDS_PER_PREFIX + 3)[row_number].as_py(),
                status=batch.column(index * _FIELDS_PER_PREFIX + 4)[row_number].as_py(),
                errors=errors,
            )


def _verify_sentence_field(
    *,
    location: str,
    text_status: object,
    language: object,
    sentences: object,
    count: object,
    status: object,
    errors: list[str],
) -> None:
    """Verify one website field's segmentation triple against its inputs."""
    if status not in SENTENCE_STATUSES:
        errors.append(f"{location} unknown sentence status {status!r}")
        return
    _verify_sentence_values(location, status, sentences, count, errors)
    _verify_sentence_provenance(location, status, text_status, language, errors)


def _verify_sentence_values(
    location: str, status: object, sentences: object, count: object, errors: list[str]
) -> None:
    """Verify the nullable sentence list and its count against the status."""
    if status == SENTENCE_SUCCESS:
        _verify_segmented_values(location, sentences, count, errors)
    elif sentences is not None or count is not None:
        errors.append(f"{location} sentence fields must be null when text was not segmented")


def _verify_segmented_values(
    location: str, sentences: object, count: object, errors: list[str]
) -> None:
    """Verify a segmented row carries a non-empty, correctly counted list."""
    if not isinstance(sentences, list) or not sentences:
        errors.append(f"{location} sentences are missing")
        return
    if count != len(sentences):
        errors.append(
            f"{location} sentence count {count} does not match {len(sentences)} sentences"
        )


def _verify_sentence_provenance(
    location: str, status: object, text_status: object, language: object, errors: list[str]
) -> None:
    """Verify the status against the text status and the detected language."""
    if text_status != _TEXT_SUCCESS:
        _verify_unfetched_text(location, status, text_status, errors)
        return
    _verify_language_coverage(location, status, language, errors)


def _verify_language_coverage(
    location: str, status: object, language: object, errors: list[str]
) -> None:
    """Verify a segmentation decision against the segmenter's language range."""
    covered = sat_code_for_glotlid_label(language) is not None
    if status == SENTENCE_SUCCESS and not covered:
        errors.append(f"{location} segmented a language {language!r} the segmenter does not cover")
    if status == SENTENCE_UNSUPPORTED_LANGUAGE and covered:
        errors.append(f"{location} rejected a covered language {language!r}")


def _verify_unfetched_text(
    location: str, status: object, text_status: object, errors: list[str]
) -> None:
    """Verify text the fetch stage did not deliver was never segmented."""
    if status == SENTENCE_SUCCESS:
        errors.append(f"{location} segmented text that was not extracted")
    elif text_status == _TEXT_ABSENT and status != SENTENCE_ABSENT:
        errors.append(f"{location} absent text must record absent sentences")


__all__ = ["verify_sentence_invariants"]
