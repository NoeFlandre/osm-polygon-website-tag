"""Tests for tag normalization and presence rules."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from osm_polygon_website_tag.domain.tags import (
    CONTACT_WEBSITE_KEY,
    WEBSITE_KEY,
    WIKIDATA_KEY,
    has_any_website,
    has_contact_website,
    has_website,
    has_wikidata,
    normalize_value,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("\t\n", ""),
        ("Hello", "Hello"),
        ("  Hello  ", "Hello"),
        (" https://example.com ", "https://example.com"),
        ("Q42", "Q42"),
        ("MiXeD", "MiXeD"),
        ("cafe\u0301", "cafe\u0301"),
        ("\u00a0text\u00a0", "text"),
        ("\u200bhidden", "hidden"),
    ],
)
def test_normalize_value(raw: str, expected: str) -> None:
    assert normalize_value(raw) == expected


@pytest.mark.parametrize("value", ["", " ", "\t\n", "\u00a0\u200b"])
def test_normalize_value_collapses_truthy_to_empty(value: str) -> None:
    assert normalize_value(value) == ""


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"website": "https://example.com"}, True),
        ({"website": "  https://example.com  "}, True),
        ({"website": ""}, False),
        ({"website": "   "}, False),
        ({}, False),
        ({"contact:website": "https://example.com"}, False),
        ({"url": "https://example.com"}, False),
        ({"website": "not-a-url"}, True),
    ],
)
def test_has_website(tags: dict[str, str], expected: bool) -> None:
    assert has_website(tags) == expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"contact:website": "https://example.com"}, True),
        ({"contact:website": "  https://example.com  "}, True),
        ({"contact:website": ""}, False),
        ({"contact:website": "   "}, False),
        ({}, False),
        # Unrelated contact:* keys are NOT picked up.
        ({"contact:phone": "+33123456789"}, False),
        ({"contact:email": "x@example.com"}, False),
        # website alone does NOT trigger has_contact_website
        ({"website": "https://example.com"}, False),
    ],
)
def test_has_contact_website(tags: dict[str, str], expected: bool) -> None:
    assert has_contact_website(tags) == expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"website": "https://example.com"}, True),
        ({"contact:website": "https://example.com"}, True),
        ({"website": "  x  ", "contact:website": "  y  "}, True),
        ({"website": "https://x.com", "contact:website": "  "}, True),
        ({"website": "  ", "contact:website": "https://x.com"}, True),
        ({"website": "   ", "contact:website": "  "}, False),
        ({"website": "  ", "contact:website": ""}, False),
        ({}, False),
    ],
)
def test_has_any_website(tags: dict[str, str], expected: bool) -> None:
    assert has_any_website(tags) == expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"wikidata": "Q42"}, True),
        ({"wikidata": "  Q42  "}, True),
        ({"wikidata": ""}, False),
        ({"wikidata": "   "}, False),
        ({}, False),
        ({"wikipedia": "en:Paris"}, False),
        ({"wikidata": "Q42;Q43"}, True),
    ],
)
def test_has_wikidata(tags: dict[str, str], expected: bool) -> None:
    assert has_wikidata(tags) == expected


def test_normalize_value_is_pure() -> None:
    """normalize_value is a pure function; same input => same output."""
    raw = "  hello  "
    fn: Callable[[str], str] = normalize_value
    assert fn(raw) == fn(raw) == "hello"


def test_normalize_value_idempotent() -> None:
    """Normalizing twice yields the same result as normalizing once."""
    raw = "  hello  "
    once = normalize_value(raw)
    twice = normalize_value(once)
    assert once == twice


def test_tag_keys_constants() -> None:
    assert WEBSITE_KEY == "website"
    assert CONTACT_WEBSITE_KEY == "contact:website"
    assert WIKIDATA_KEY == "wikidata"
