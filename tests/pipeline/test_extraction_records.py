"""Contracts for pure extraction-record construction."""

from __future__ import annotations

import datetime as dt

import pytest

from osm_polygon_website_tag.pipeline import extraction, extraction_records
from osm_polygon_website_tag.pipeline.extraction_records import (
    __all__ as extraction_record_exports,
)
from osm_polygon_website_tag.pipeline.extraction_records import (
    build_comparison_record,
    build_public_record,
    build_rejection_record,
)
from osm_polygon_website_tag.pipeline.record_builders import derive_tags, derive_wikidata

_TS = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def test_extraction_records_module_exposes_focused_boundary() -> None:
    assert set(extraction_record_exports) == {
        "build_comparison_record",
        "build_public_record",
        "build_rejection_record",
    }


def test_extraction_preserves_record_builder_compatibility_surface() -> None:
    assert extraction._public_record is build_public_record
    assert extraction._comparison_record is build_comparison_record
    assert extraction._rejection_record is build_rejection_record


def test_public_record_does_not_invoke_wikidata_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public builder must not call the comparison/rejection-only helper."""

    def _boom(*_args: object, **_kwargs: object) -> tuple[str | None, bool]:
        raise AssertionError("public records must not invoke the Wikidata helper")

    monkeypatch.setattr(extraction_records, "derive_wikidata", _boom)
    record = build_public_record(
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
    """Replace the four URL helpers in the record module with raisers."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("comparison/rejection must not invoke URL helpers")

    for name in (
        "classify_website",
        "classify_contact_website",
        "extract_hostname",
        "extract_contact_hostname",
    ):
        monkeypatch.setattr(extraction_records, name, _boom)


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
    record = build_comparison_record(
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
    record = build_rejection_record(
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
    return build_public_record(
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
    comparison = build_comparison_record(
        source_pbf="s.osm.pbf",
        region="r",
        tags_dict=tags,
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=_TS,
    )
    rejection = build_rejection_record(
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
