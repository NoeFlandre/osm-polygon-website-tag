"""Public website-text schema and word-count contracts."""

from __future__ import annotations

import pyarrow as pa

from osm_polygon_website_tag.contracts.text_schema import (
    TEXT_COLUMN_NAMES,
    TEXT_STATUSES,
    TEXT_TERMINAL_STATUSES,
    count_words,
    initial_text_fields,
    status_has_retryable_value,
)


def test_text_columns_cover_both_osm_tags() -> None:
    assert TEXT_COLUMN_NAMES == (
        "website_text",
        "website_word_count",
        "website_text_status",
        "contact_website_text",
        "contact_website_word_count",
        "contact_website_text_status",
    )


def test_status_vocabulary_is_frozen() -> None:
    assert (
        frozenset(
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
        == TEXT_STATUSES
    )


def test_terminal_status_contract_matches_resume_semantics() -> None:
    assert frozenset({"absent", "success"}) == TEXT_TERMINAL_STATUSES
    assert not status_has_retryable_value(pa.array(["success", "absent"]))
    assert status_has_retryable_value(pa.array(["success", "fetch_error"]))
    assert status_has_retryable_value(pa.array([None], type=pa.string()))


def test_count_words_uses_unicode_word_sequences() -> None:
    assert count_words("Bonjour l'été — 東京 2026") == 5


def test_initial_fields_are_independent_for_both_tags() -> None:
    assert initial_text_fields(website_present=True, contact_website_present=False) == {
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "pending",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def test_full_text_is_not_truncated() -> None:
    text = "word " * 1_000_000
    assert count_words(text) == 1_000_000
