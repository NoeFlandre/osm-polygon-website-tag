"""Website-text schema fields, status priorities, and deterministic word counting."""

from __future__ import annotations

import re
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

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

# Only these values mean that no further URL work is required for a row.
TEXT_TERMINAL_STATUSES = frozenset({"absent", "success"})

# ``TEXT_NULL_STATUS`` is the persisted summary sentinel for a null Arrow
# status. The remaining sets classify every nonterminal status for deterministic
# resume prioritization; unknown values remain retryable by policy.
TEXT_NULL_STATUS = "__null__"
TEXT_UNFINISHED_STATUSES = frozenset({"pending", TEXT_NULL_STATUS})
TEXT_TRANSIENT_STATUSES = frozenset({"empty", "fetch_error", "extract_error"})
TEXT_DETERMINISTIC_STATUSES = frozenset({"invalid_url", "unsafe_url"})

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


def status_has_retryable_value(status: pa.Array) -> bool:
    """Return whether a status column contains null or a nonterminal value.

    The workflow resume check and the dataset-card completion count share this
    Arrow-level contract so a failed, empty, unsafe, or otherwise unknown URL
    result cannot be reported as a completed source.
    """
    terminal: Any = None
    for expected in sorted(TEXT_TERMINAL_STATUSES):
        match = _arrow_kernel("equal", status, expected)
        terminal = match if terminal is None else _arrow_kernel("or_kleene", terminal, match)
    retryable = pc.fill_null(_arrow_kernel("invert", terminal), True)
    return bool(_arrow_kernel("any", retryable).as_py() or False)


def _arrow_kernel(name: str, *args: Any) -> Any:
    """Call a dynamically registered Arrow kernel while keeping ty strict."""
    return pc.call_function(name, list(args))


__all__ = [
    "TEXT_COLUMN_NAMES",
    "TEXT_DETERMINISTIC_STATUSES",
    "TEXT_FIELDS",
    "TEXT_NULL_STATUS",
    "TEXT_STATUSES",
    "TEXT_TERMINAL_STATUSES",
    "TEXT_TRANSIENT_STATUSES",
    "TEXT_UNFINISHED_STATUSES",
    "count_words",
    "initial_text_fields",
    "status_has_retryable_value",
]
