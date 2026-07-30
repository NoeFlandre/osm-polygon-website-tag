"""Tests for primary category selection."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.categories import (
    CATEGORY_ORDER,
    normalize_category,
    select_primary_category,
)


def test_category_order_is_stable() -> None:
    """The category precedence order is frozen and must not change silently."""
    assert CATEGORY_ORDER[:5] == ("boundary", "building", "amenity", "shop", "tourism")
    assert "other" in CATEGORY_ORDER


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"amenity": "restaurant"}, "amenity"),
        ({"building": "yes"}, "building"),
        ({"building": "yes", "amenity": "restaurant"}, "building"),
        ({"landuse": "forest"}, "landuse"),
        ({"leisure": "park"}, "leisure"),
        ({"natural": "water"}, "natural"),
        ({"shop": "bakery"}, "shop"),
        ({"tourism": "hotel"}, "tourism"),
        ({"historic": "castle"}, "historic"),
        ({"place": "city"}, "place"),
        ({"aeroway": "aerodrome"}, "aeroway"),
        ({"waterway": "river"}, "waterway"),
        ({"highway": "pedestrian"}, "highway"),
        ({"man_made": "pier"}, "man_made"),
        ({"boundary": "administrative"}, "boundary"),
        ({"name": "Foo"}, "other"),
        ({}, "other"),
        ({"amenity": "restaurant", "shop": "bakery"}, "amenity"),
        ({"shop": "bakery", "tourism": "hotel"}, "shop"),
        ({"building": "yes", "highway": "pedestrian"}, "building"),
    ],
)
def test_select_primary_category(tags: dict[str, str], expected: str) -> None:
    assert select_primary_category(tags) == expected


def test_select_primary_category_is_deterministic() -> None:
    tags = {"amenity": "restaurant", "building": "yes"}
    assert select_primary_category(tags) == select_primary_category(tags)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Restaurant", "restaurant"),
        ("  Restaurant  ", "restaurant"),
        ("RETAIL", "retail"),
        ("café", "café"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_category(value: str, expected: str) -> None:
    assert normalize_category(value) == expected
