"""Tests for the shared extraction tag projection.

These tests pin the refactor that extracts the duplicated tag-derived logic
from the extraction row builders into ``pipeline/record_builders.py``.

The shared projection (:func:`derive_tags`) computes only the six values
genuinely reused by all three row builders:

* normalized ``website`` / ``contact:website``
* ``has_website`` / ``has_contact_website`` / ``has_any_website``
* ``primary_category``

Wikidata normalization and presence live in the separate
:func:`derive_wikidata` helper used by the comparison and rejection builders.
Row-construction contracts live in ``test_extraction_records.py``.
"""

from __future__ import annotations

import dataclasses

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
from osm_polygon_website_tag.pipeline.record_builders import (
    DerivedTags,
    derive_tags,
    derive_wikidata,
)


def _expected_normalized(tags: dict[str, str], key: str) -> str | None:
    """Reference implementation of the normalized-or-None projection."""
    return normalize_value(tags.get(key, "")) or None


# ---------------------------------------------------------------------------
# derive_tags matrix
# ---------------------------------------------------------------------------

_MATRIX: list[tuple[str, dict[str, str], str | None, str | None]] = [
    (
        "website_only",
        {"website": "https://example.com", "building": "yes"},
        "https://example.com",
        None,
    ),
    (
        "contact_only",
        {"contact:website": "https://c.example", "amenity": "cafe"},
        None,
        "https://c.example",
    ),
    (
        "both",
        {
            "website": "https://w.example",
            "contact:website": "https://c.example",
            "building": "yes",
        },
        "https://w.example",
        "https://c.example",
    ),
    (
        "absent",
        {"building": "yes"},
        None,
        None,
    ),
    (
        "whitespace_only",
        {"website": "   ", "contact:website": "\t", "building": "yes"},
        None,
        None,
    ),
    (
        "invisible_unicode",
        {"website": "\u00a0\u200b", "contact:website": "\ufeff"},
        None,
        None,
    ),
]


@pytest.mark.parametrize(
    ("name", "tags", "website", "contact_website"),
    _MATRIX,
    ids=[case[0] for case in _MATRIX],
)
def test_derive_tags_normalized_values_and_flags(
    name: str,
    tags: dict[str, str],
    website: str | None,
    contact_website: str | None,
) -> None:
    derived = derive_tags(tags)

    assert derived.website == website
    assert derived.contact_website == contact_website
    assert derived.has_website == (website is not None)
    assert derived.has_contact_website == (contact_website is not None)
    assert derived.has_any_website == (website is not None or contact_website is not None)


@pytest.mark.parametrize(
    ("name", "tags", "_website", "_contact_website"),
    _MATRIX,
    ids=[case[0] for case in _MATRIX],
)
def test_derive_tags_flags_match_domain_functions(
    name: str,
    tags: dict[str, str],
    _website: object,
    _contact_website: object,
) -> None:
    """The projection's flags must equal the current domain presence rules."""
    derived = derive_tags(tags)

    assert derived.has_website is has_website(tags)
    assert derived.has_contact_website is has_contact_website(tags)
    assert derived.has_any_website is has_any_website(tags)
    # Normalized values must equal the current ``normalize_value(...) or None``.
    assert derived.website == _expected_normalized(tags, WEBSITE_KEY)
    assert derived.contact_website == _expected_normalized(tags, CONTACT_WEBSITE_KEY)


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"amenity": "cafe", "building": "yes"}, "building"),
        ({"natural": "wood", "leisure": "park"}, "leisure"),
        ({"highway": "residential"}, "highway"),
        ({"foo": "bar"}, "other"),
        ({}, "other"),
        # Whitespace-only category value is ignored -> falls through.
        ({"building": "   ", "amenity": "cafe"}, "amenity"),
    ],
)
def test_derive_tags_primary_category_is_deterministic(tags: dict[str, str], expected: str) -> None:
    assert derive_tags(tags).primary_category == expected


def test_derive_tags_returns_frozen_value_object() -> None:
    derived = derive_tags({"website": "https://example.com"})
    assert isinstance(derived, DerivedTags)
    with pytest.raises(dataclasses.FrozenInstanceError):
        derived.website = "mutated"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# ---------------------------------------------------------------------------
# Wikidata helper (used only by comparison / rejection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tags", "wikidata"),
    [
        ("present", {"wikidata": "Q42"}, "Q42"),
        ("absent", {"building": "yes"}, None),
        ("whitespace_only", {"wikidata": "   "}, None),
        ("invisible_unicode", {"wikidata": "\u00a0\u200b"}, None),
    ],
    ids=["present", "absent", "whitespace_only", "invisible_unicode"],
)
def test_derive_wikidata_normalized_value_and_flag(
    name: str, tags: dict[str, str], wikidata: str | None
) -> None:
    value, has = derive_wikidata(tags)
    assert value == wikidata
    assert has is (wikidata is not None)


@pytest.mark.parametrize(
    "tags",
    [
        {"wikidata": "Q42"},
        {"wikidata": "Q42", "building": "yes"},
        {"building": "yes"},
        {"wikidata": "   "},
        {"wikidata": "\u200b"},
    ],
)
def test_derive_wikidata_flag_matches_domain_function(tags: dict[str, str]) -> None:
    _value, has = derive_wikidata(tags)
    assert has is has_wikidata(tags)
    # The normalized value must equal the canonical projection.
    assert _value == _expected_normalized(tags, WIKIDATA_KEY)
