"""Sentence-segmentation fields for public polygon schema v1.5."""

from __future__ import annotations

import pyarrow as pa

SENTENCE_SCHEMA_VERSION = "v1.5"

SENTENCE_SUCCESS = "success"
SENTENCE_ABSENT = "absent"
SENTENCE_UNSUPPORTED_LANGUAGE = "unsupported_language"
SENTENCE_EMPTY_TEXT = "empty_text"

# The segmentation model covers far fewer languages than the detector, so a row
# whose detected language is out of range is recorded explicitly rather than
# left indistinguishable from text that was never segmented.
SENTENCE_STATUSES = frozenset(
    {
        SENTENCE_SUCCESS,
        SENTENCE_ABSENT,
        SENTENCE_UNSUPPORTED_LANGUAGE,
        SENTENCE_EMPTY_TEXT,
    }
)

SENTENCE_COLUMN_NAMES = (
    "website_sentences",
    "website_sentence_count",
    "website_sentence_status",
    "contact_website_sentences",
    "contact_website_sentence_count",
    "contact_website_sentence_status",
)

_SENTENCE_LIST_TYPE = pa.list_(pa.field("item", pa.string(), nullable=False))

SENTENCE_FIELDS = (
    pa.field("website_sentences", _SENTENCE_LIST_TYPE, nullable=True),
    pa.field("website_sentence_count", pa.int32(), nullable=True),
    pa.field("website_sentence_status", pa.string(), nullable=False),
    pa.field("contact_website_sentences", _SENTENCE_LIST_TYPE, nullable=True),
    pa.field("contact_website_sentence_count", pa.int32(), nullable=True),
    pa.field("contact_website_sentence_status", pa.string(), nullable=False),
)


__all__ = [
    "SENTENCE_ABSENT",
    "SENTENCE_COLUMN_NAMES",
    "SENTENCE_EMPTY_TEXT",
    "SENTENCE_FIELDS",
    "SENTENCE_SCHEMA_VERSION",
    "SENTENCE_STATUSES",
    "SENTENCE_SUCCESS",
    "SENTENCE_UNSUPPORTED_LANGUAGE",
]
