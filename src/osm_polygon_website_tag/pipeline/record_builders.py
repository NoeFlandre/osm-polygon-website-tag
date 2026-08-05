"""Shared normalized tag projection for extraction row builders.

The three row builders in :mod:`osm_polygon_website_tag.pipeline.extraction`
(``_public_record``, ``_comparison_record``, ``_rejection_record``) each
independently normalized the same OSM tags and selected the primary
category. This module factors out the values genuinely reused by all three
into a single frozen value object. Production extraction computes it once per
area payload and passes it to the builders; direct builder calls can omit it
and use the same derive-on-demand fallback.

Scope
-----

:func:`derive_tags` computes exactly the six shared values:

* normalized ``website`` / ``contact:website``
* ``has_website`` / ``has_contact_website`` / ``has_any_website``
* ``primary_category``

Wikidata normalization and presence live in a separate small helper
(:func:`derive_wikidata`) used only by ``_comparison_record`` and
``_rejection_record``.

URL classification and hostname extraction are **public-row-only**: they
belong only to the public shard and would be a processing regression if
applied to every comparison and rejection row. They remain in
``_public_record``.

Equivalence
-----------

The presence flags are derived from the normalized values, which is identical
to both former implementations:

* ``normalize_value(absent_or_whitespace) == ""`` -> ``... or None`` -> ``None``
* ``has_*(tags)`` == ``normalize_value(...) != ""``

So ``website is not None`` equals :func:`has_website` for present, absent, and
whitespace-only keys. Pure functions only; no I/O, no global state.
"""

from __future__ import annotations

from dataclasses import dataclass

from osm_polygon_website_tag.domain.categories import select_primary_category
from osm_polygon_website_tag.domain.tags import (
    CONTACT_WEBSITE_KEY,
    WEBSITE_KEY,
    WIKIDATA_KEY,
    normalize_value,
)


@dataclass(frozen=True)
class DerivedTags:
    """Shared normalized tag values and presence flags for one OSM object."""

    website: str | None
    contact_website: str | None
    has_website: bool
    has_contact_website: bool
    has_any_website: bool
    primary_category: str


def derive_tags(tags: dict[str, str]) -> DerivedTags:
    """Project one OSM tag dictionary into the shared derived values.

    Each key is normalized once and the presence flags are derived from those
    normalized values. ``primary_category`` is selected from the frozen
    precedence list in :mod:`osm_polygon_website_tag.domain.categories`.
    """
    website = normalize_value(tags.get(WEBSITE_KEY, "")) or None
    contact_website = normalize_value(tags.get(CONTACT_WEBSITE_KEY, "")) or None
    has_website = website is not None
    has_contact_website = contact_website is not None
    return DerivedTags(
        website=website,
        contact_website=contact_website,
        has_website=has_website,
        has_contact_website=has_contact_website,
        has_any_website=has_website or has_contact_website,
        primary_category=select_primary_category(tags),
    )


def derive_wikidata(tags: dict[str, str]) -> tuple[str | None, bool]:
    """Normalize ``wikidata`` once and return ``(value, has_value)``.

    Used only by ``_comparison_record`` and ``_rejection_record``; the public
    shard's v1.3 schema omits Wikidata.
    """
    wikidata = normalize_value(tags.get(WIKIDATA_KEY, "")) or None
    return wikidata, wikidata is not None


__all__ = ["DerivedTags", "derive_tags", "derive_wikidata"]
