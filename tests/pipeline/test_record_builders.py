"""Tests for the shared extraction tag projection and row builders.

These tests pin the refactor that extracts the duplicated tag-derived logic
from ``pipeline/extraction.py`` into ``pipeline/record_builders.py``.

The shared projection (:func:`derive_tags`) computes only the six values
genuinely reused by all three row builders:

* normalized ``website`` / ``contact:website``
* ``has_website`` / ``has_contact_website`` / ``has_any_website``
* ``primary_category``

Wikidata normalization and presence live in a separate small helper
(:func:`derive_wikidata`) used only by ``_comparison_record`` and
``_rejection_record``.

URL classification and hostname extraction are public-row-only and must
never run for comparison or rejection rows.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

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
from osm_polygon_website_tag.pipeline import extraction, record_builders
from osm_polygon_website_tag.pipeline.extraction import (
    _comparison_record,
    _public_record,
    _rejection_record,
)
from osm_polygon_website_tag.pipeline.record_builders import (
    DerivedTags,
    derive_tags,
    derive_wikidata,
)

_TS = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


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


def test_public_record_does_not_invoke_wikidata_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_public_record`` must not call the comparison/rejection-only Wikidata helper."""

    def _boom(*_args: object, **_kwargs: object) -> tuple[str | None, bool]:
        raise AssertionError("_public_record must not invoke the Wikidata helper")

    monkeypatch.setattr(record_builders, "derive_wikidata", _boom)
    record = _public_record(
        polygon_id="s:way/1",
        source_pbf="s.osm.pbf",
        region="r",
        tags_dict={
            "website": "https://example.com",
            "wikidata": "Q42",
            "building": "yes",
        },
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=_TS,
        geom_text="{}",
        centroid_text="{}",
        centroid_kind="lambert_azimuthal_equal_area",
        lat=0.0,
        lon=0.0,
        bbox=[0.0, 0.0, 1.0, 1.0],
        area_m2=1.0,
        area_bucket="<10m2",
    )
    # Public v1.3 schema carries no ``wikidata`` column.
    assert "wikidata" not in record


# ---------------------------------------------------------------------------
# Comparison / rejection must not invoke URL classification or hostnames
# ---------------------------------------------------------------------------


def _patched_to_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the four URL helpers on the extraction module with raisers."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("comparison/rejection must not invoke URL helpers")

    for name in (
        "classify_website",
        "classify_contact_website",
        "extract_hostname",
        "extract_contact_hostname",
    ):
        monkeypatch.setattr(extraction, name, _boom)


def _tags_with_both_keys() -> dict[str, str]:
    return {
        "website": "https://w.example",
        "contact:website": "https://c.example",
        "wikidata": "Q42",
        "building": "yes",
    }


def test_comparison_record_does_not_invoke_url_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_to_raise(monkeypatch)
    record = _comparison_record(
        source_pbf="s.osm.pbf",
        region="r",
        tags_dict=_tags_with_both_keys(),
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=_TS,
    )
    # Sanity: the row was actually built and carries shared values.
    assert record["website"] == "https://w.example"
    assert "website_class" not in record
    assert "website_hostname" not in record


def test_rejection_record_does_not_invoke_url_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_to_raise(monkeypatch)
    record = _rejection_record(
        source_pbf="s.osm.pbf",
        region="r",
        tags_dict=_tags_with_both_keys(),
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=_TS,
        candidate_kind="closed_way",
        rejection_kind="no_area_callback",
        message="m",
    )
    assert record["website"] == "https://w.example"
    assert "website_class" not in record
    assert "website_hostname" not in record


# ---------------------------------------------------------------------------
# Public output still carries exact URL classes and hostnames (literal values)
# ---------------------------------------------------------------------------


def _public(tags: dict[str, str]) -> dict[str, object]:
    return _public_record(
        polygon_id="s:way/1",
        source_pbf="s.osm.pbf",
        region="r",
        tags_dict=tags,
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=_TS,
        geom_text="{}",
        centroid_text="{}",
        centroid_kind="lambert_azimuthal_equal_area",
        lat=0.0,
        lon=0.0,
        bbox=[0.0, 0.0, 1.0, 1.0],
        area_m2=1.0,
        area_bucket="<10m2",
    )


@pytest.mark.parametrize(
    ("website", "expected_class", "expected_hostname"),
    [
        ("https://example.com/path", "absolute_url", "example.com"),
        ("example.com", "bare_hostname", "example.com"),
        ("https://a.example; https://b.example", "multiple", None),
        ("???", "malformed", None),
    ],
)
def test_public_record_url_class_and_hostname_are_exact(
    website: str, expected_class: str, expected_hostname: str | None
) -> None:
    record = _public({"website": website, "building": "yes"})
    assert record["website_class"] == expected_class
    assert record["website_hostname"] == expected_hostname


def test_public_record_contact_url_class_and_hostname_are_exact() -> None:
    record = _public({"contact:website": "https://c.example/x", "building": "yes"})
    assert record["contact_website_class"] == "absolute_url"
    assert record["contact_website_hostname"] == "c.example"
    # Absent website key -> null class/hostname, has_website False.
    assert record["website_class"] is None
    assert record["website_hostname"] is None
    assert record["has_website"] is False


def test_public_record_text_status_reflects_shared_flags() -> None:
    record = _public({"contact:website": "https://c.example", "building": "yes"})
    assert record["website_text_status"] == "absent"
    assert record["contact_website_text_status"] == "pending"


# ---------------------------------------------------------------------------
# All three builders share identical derived values for one tag dictionary
# ---------------------------------------------------------------------------


def test_three_builders_share_identical_derived_values() -> None:
    tags = _tags_with_both_keys()
    derived = derive_tags(tags)

    public = _public(tags)
    comparison = _comparison_record(
        source_pbf="s.osm.pbf",
        region="r",
        tags_dict=tags,
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=_TS,
    )
    rejection = _rejection_record(
        source_pbf="s.osm.pbf",
        region="r",
        tags_dict=tags,
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=_TS,
        candidate_kind="closed_way",
        rejection_kind="no_area_callback",
        message="m",
    )

    # The public v1.3 schema omits wikidata/has_wikidata; compare the fields
    # common to all three shards first.
    for key, derived_value in [
        ("website", derived.website),
        ("contact_website", derived.contact_website),
        ("has_website", derived.has_website),
        ("has_contact_website", derived.has_contact_website),
        ("has_any_website", derived.has_any_website),
    ]:
        assert public[key] == derived_value
        assert comparison[key] == derived_value
        assert rejection[key] == derived_value

    # wikidata / has_wikidata are shared by comparison and rejection only.
    wikidata_value, has_wikidata = derive_wikidata(tags)
    assert comparison["wikidata"] == wikidata_value
    assert comparison["has_wikidata"] is has_wikidata
    assert rejection["wikidata"] == wikidata_value
    assert rejection["has_wikidata"] is has_wikidata

    # Public exposes the same category under its own column name.
    assert public["osm_primary_tag"] == derived.primary_category
    assert comparison["primary_category"] == derived.primary_category
    assert rejection["primary_category"] == derived.primary_category
