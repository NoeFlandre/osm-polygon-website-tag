"""Website-text schema fields and deterministic word counting."""

from __future__ import annotations

import re

import pyarrow as pa

TEXT_STATUSES = frozenset(
    {
        "absent",
        "pending",
        "success",
        "empty",
        "invalid_url",
        "unsafe_url",
        "fetch_error",
        "extract_error",
    }
)

TEXT_COLUMN_NAMES = (
    "website_text",
    "website_word_count",
    "website_text_status",
    "contact_website_text",
    "contact_website_word_count",
    "contact_website_text_status",
)

TEXT_FIELDS = (
    pa.field("website_text", pa.large_string(), nullable=True),
    pa.field("website_word_count", pa.int64(), nullable=True),
    pa.field("website_text_status", pa.string(), nullable=False),
    pa.field("contact_website_text", pa.large_string(), nullable=True),
    pa.field("contact_website_word_count", pa.int64(), nullable=True),
    pa.field("contact_website_text_status", pa.string(), nullable=False),
)


def count_words(text: str) -> int:
    """Count Unicode word sequences using Python's ``\\w+`` definition."""
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def initial_text_fields(
    *,
    website_present: bool,
    contact_website_present: bool,
) -> dict[str, object]:
    """Return pending/absent enrichment fields for a newly extracted row."""
    return {
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "pending" if website_present else "absent",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "pending" if contact_website_present else "absent",
    }


__all__ = [
    "TEXT_COLUMN_NAMES",
    "TEXT_FIELDS",
    "TEXT_STATUSES",
    "count_words",
    "initial_text_fields",
]
