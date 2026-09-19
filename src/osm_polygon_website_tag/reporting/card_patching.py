"""Byte-preserving section patchers for legacy dataset cards."""

from __future__ import annotations

import re

from osm_polygon_website_tag.reporting.card_rendering import (
    _render_geographic_section,
    _render_language_section,
    _render_polygon_geometry_section,
    _render_website_text_section,
)
from osm_polygon_website_tag.reporting.card_stats import CardStats
from osm_polygon_website_tag.reporting.geometry_stats import GeometryStats

_TOP_LEVEL_HEADING = re.compile(rb"(?m)^## [^\r\n]*(?:\r\n|\n|$)")
_WEBSITE_TEXT_HEADING = re.compile(rb"(?m)^## Website text(?:\r\n|\n|$)")
_LANGUAGE_HEADING = re.compile(rb"(?m)^## Languages(?:\r\n|\n|$)")
_GEOMETRY_HEADING = re.compile(rb"(?m)^## Polygon geometry(?:\r\n|\n|$)")
_GEOGRAPHIC_HEADING = re.compile(rb"(?m)^## Geographic distribution(?:\r\n|\n|$)")


def _update_geometry_section(card: bytes, geometry: GeometryStats) -> bytes:
    """Replace or insert one geometry block without rewriting other card bytes."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    block = _geometry_block_bytes(geometry, newline)
    existing = _GEOMETRY_HEADING.search(card)
    if existing is not None:
        following = _TOP_LEVEL_HEADING.search(card, existing.end())
        end = following.start() if following is not None else len(card)
        return card[: existing.start()] + block + card[end:]

    insertion = _GEOGRAPHIC_HEADING.search(card)
    if insertion is not None:
        return card[: insertion.start()] + block + card[insertion.start() :]
    return _append_geometry_block(card, block, newline)


def _update_geographic_section(card: bytes, stats: CardStats) -> bytes:
    """Replace or insert the geography block using the card's newline style."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    block = newline.join(line.encode("utf-8") for line in _render_geographic_section(stats))
    block += newline
    existing = _GEOGRAPHIC_HEADING.search(card)
    if existing is not None:
        return _replace_section(card, existing, block)

    return _insert_geographic_section(card, block, newline)


def _insert_geographic_section(card: bytes, block: bytes, newline: bytes) -> bytes:
    """Insert geography after geometry or append it to the card."""
    geometry = _GEOMETRY_HEADING.search(card)
    if geometry is not None:
        return _insert_after_section(card, geometry, block)
    return _append_geometry_block(card, block, newline)


def _update_website_text_section(card: bytes, stats: CardStats) -> bytes:
    """Replace the generated website-text block without adding it to legacy cards."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    existing = _WEBSITE_TEXT_HEADING.search(card)
    if existing is None:
        return card
    block = newline.join(line.encode("utf-8") for line in _render_website_text_section(stats))
    return _replace_section(card, existing, block + newline)


def _update_language_section(card: bytes, stats: CardStats) -> bytes:
    """Replace the generated language block with canonical population totals."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    existing = _LANGUAGE_HEADING.search(card)
    if existing is not None:
        return _replace_existing_language_section(card, existing, stats, newline)
    if not stats.detected_language_count:
        return card
    return _insert_new_language_section(card, stats, newline)


def _replace_existing_language_section(
    card: bytes,
    existing: re.Match[bytes],
    stats: CardStats,
    newline: bytes,
) -> bytes:
    """Replace or remove an existing generated language section."""
    if not stats.detected_language_count:
        return _replace_section(card, existing, b"")
    return _replace_section(card, existing, _language_section_block(stats, newline))


def _insert_new_language_section(card: bytes, stats: CardStats, newline: bytes) -> bytes:
    """Insert a missing generated language section after website text."""
    website = _WEBSITE_TEXT_HEADING.search(card)
    if website is not None:
        return _insert_after_section(card, website, _language_section_block(stats, newline))
    return _append_geometry_block(card, _language_section_block(stats, newline), newline)


def _language_section_block(stats: CardStats, newline: bytes) -> bytes:
    """Render one language section using the card's newline convention."""
    return newline.join(line.encode("utf-8") for line in _render_language_section(stats)) + newline


def _replace_section(card: bytes, heading: re.Match[bytes], block: bytes) -> bytes:
    """Replace a headed card section through the next top-level heading."""
    following = _TOP_LEVEL_HEADING.search(card, heading.end())
    end = following.start() if following is not None else len(card)
    return card[: heading.start()] + block + card[end:]


def _insert_after_section(card: bytes, heading: re.Match[bytes], block: bytes) -> bytes:
    """Insert a derived section after an existing headed section."""
    following = _TOP_LEVEL_HEADING.search(card, heading.end())
    insertion = following.start() if following is not None else len(card)
    return card[:insertion] + block + card[insertion:]


def _geometry_block_bytes(geometry: GeometryStats, newline: bytes) -> bytes:
    """Render the additive block using the existing card's newline convention."""
    lines = _render_polygon_geometry_section(geometry)
    return newline.join(line.encode("utf-8") for line in lines) + newline


def _append_geometry_block(card: bytes, block: bytes, newline: bytes) -> bytes:
    """Append a geometry block while retaining the existing card bytes."""
    prefix = card
    if prefix and not prefix.endswith(newline):
        prefix += newline
    if prefix:
        prefix += newline
    return prefix + block
