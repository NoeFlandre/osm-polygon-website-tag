"""Tests for region detection from PBF filenames."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.region import region_from_pbf_filename


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("afghanistan-latest.osm.pbf", "afghanistan"),
        ("monaco-latest.osm.pbf", "monaco"),
        ("rhone-alpes-latest.osm.pbf", "rhone-alpes"),
        ("american-oceania-latest.osm.pbf", "american-oceania"),
        ("europe-latest.osm.pbf", "europe"),
        ("planet-latest.osm.pbf", "planet"),
        ("a.osm.pbf", "a"),
        ("foo.osm", "foo"),
        ("foo.pbf", "foo"),
    ],
)
def test_region_from_pbf_filename(filename: str, expected: str) -> None:
    assert region_from_pbf_filename(filename) == expected


def test_region_is_deterministic() -> None:
    assert region_from_pbf_filename("monaco-latest.osm.pbf") == region_from_pbf_filename(
        "monaco-latest.osm.pbf"
    )


def test_region_normalises_case() -> None:
    assert region_from_pbf_filename("Monaco-latest.osm.pbf") == "monaco"


def test_region_strips_trailing_dash_latest() -> None:
    assert region_from_pbf_filename("monaco-latest.osm.pbf") == "monaco"


def test_region_handles_region_only_filename() -> None:
    assert region_from_pbf_filename("rhone-alpes-latest.osm.pbf") == "rhone-alpes"
