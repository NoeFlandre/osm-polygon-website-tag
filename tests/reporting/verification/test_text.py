"""Focused contracts for private text verification helpers."""

from __future__ import annotations

from osm_polygon_website_tag.reporting.verification import text


def test_text_verification_helpers_cover_terminal_and_absent_states() -> None:
    assert text._absent_text_is_consistent(None, None, "absent")
    assert not text._absent_text_is_consistent("x", None, "absent")
    assert text._empty_text_is_consistent("", 0)
    assert not text._empty_text_is_consistent(None, 0)
    errors: list[str] = []
    text._verify_one_text_value(
        tag_value=None,
        text=None,
        word_count=None,
        text_status="absent",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )
    text._verify_one_text_value(
        tag_value="https://example.org",
        text="one two",
        word_count=2,
        text_status="success",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )
    assert errors == []
    text._verify_one_text_value(
        tag_value="https://example.org",
        text=None,
        word_count=None,
        text_status="pending",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )
    assert any("remains pending" in error for error in errors)
    text._verify_text_row(
        {
            "website": "https://example.org",
            "website_text": "one two",
            "website_word_count": 2,
            "website_text_status": "success",
            "contact_website": None,
            "contact_website_text": None,
            "contact_website_word_count": None,
            "contact_website_text_status": "absent",
        },
        "a.parquet",
        False,
        [],
    )
