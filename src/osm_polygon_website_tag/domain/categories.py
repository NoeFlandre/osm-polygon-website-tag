"""Primary category selection for OSM polygons.

Pure functions only. No I/O, no global state.

Categories are derived from a fixed precedence list of OSM top-level keys.
The first key that appears in the polygon's tag set (after whitespace
trimming) wins. The category name is the lowercased OSM key; the sub-value
is exposed separately via ``normalize_category`` but does not influence the
primary breakdown.
"""

from __future__ import annotations

# Frozen precedence order. The first match wins. Add new categories at the
# end; never reorder in place.
CATEGORY_ORDER: tuple[str, ...] = (
    "boundary",
    "building",
    "amenity",
    "shop",
    "tourism",
    "historic",
    "leisure",
    "landuse",
    "natural",
    "waterway",
    "aeroway",
    "place",
    "highway",
    "man_made",
    "public_transport",
    "railway",
    "power",
    "other",
)

# Pre-build the precedence rank for O(1) lookup.
_RANK: dict[str, int] = {key: i for i, key in enumerate(CATEGORY_ORDER)}


def normalize_category(value: str | None) -> str:
    """Return the lowercased, trimmed category value."""
    if value is None:
        return ""
    return value.strip().lower()


def select_primary_category(tags: dict[str, str]) -> str:
    """Return the broadest applicable OSM category for ``tags``.

    The category is the lowercased OSM top-level key. The fixed order is
    defined in ``CATEGORY_ORDER``. Tags with empty values are ignored. If no
    category key is present, ``"other"`` is returned.
    """
    for key in CATEGORY_ORDER:
        if key == "other":
            continue
        value = tags.get(key)
        if value is None:
            continue
        if value.strip():
            return key
    return "other"
