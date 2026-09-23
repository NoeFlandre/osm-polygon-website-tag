"""Byte-preserving section patchers for legacy dataset cards.

Only the stats-derived sections in ``_PATCHED_SECTION_ORDER`` are refreshed.
A missing section is inserted where ``render_markdown`` would place it: after
the nearest earlier patched section present, else before the nearest later
one, else appended. The snapshot ("At a glance"), hostname, and method
sections also depend on ``CardStats`` but are frozen on legacy cards: a
patched card keeps their original counts. Regenerate the card with the full
builder (through a proper release) when those must change.
"""

from __future__ import annotations

import re

from osm_polygon_website_tag.reporting.card_rendering import (
    _render_geographic_section,
    _render_language_section,
    _render_polygon_geometry_section,
    _render_sentence_section,
    _render_website_text_section,
)
from osm_polygon_website_tag.reporting.card_stats import CardStats
from osm_polygon_website_tag.reporting.geometry_stats import GeometryStats

_TOP_LEVEL_HEADING = re.compile(rb"(?m)^## [^\r\n]*(?:\r\n|\n|$)")
_WEBSITE_TEXT_HEADING = re.compile(rb"(?m)^## Website text(?:\r\n|\n|$)")
_LANGUAGE_HEADING = re.compile(rb"(?m)^## Languages(?:\r\n|\n|$)")
_SENTENCE_HEADING = re.compile(rb"(?m)^## Sentences(?:\r\n|\n|$)")
_GEOMETRY_HEADING = re.compile(rb"(?m)^## Polygon geometry(?:\r\n|\n|$)")
_GEOGRAPHIC_HEADING = re.compile(rb"(?m)^## Geographic distribution(?:\r\n|\n|$)")
# Must match the order of these sections in ``card_rendering.render_markdown``.
_PATCHED_SECTION_ORDER = (
    _WEBSITE_TEXT_HEADING,
    _LANGUAGE_HEADING,
    _SENTENCE_HEADING,
    _GEOGRAPHIC_HEADING,
    _GEOMETRY_HEADING,
)


def update_geometry_section(card: bytes, geometry: GeometryStats) -> bytes:
    """Replace or insert one geometry block without rewriting other card bytes."""
    newline = _newline(card)
    block = _geometry_block_bytes(geometry, newline)
    existing = _GEOMETRY_HEADING.search(card)
    if existing is not None:
        return _replace_section(card, existing, block)
    return _insert_section(card, _GEOMETRY_HEADING, block, newline)


def update_geographic_section(card: bytes, stats: CardStats) -> bytes:
    """Replace or insert the geography block using the card's newline style."""
    newline = _newline(card)
    block = _block_bytes(_render_geographic_section(stats), newline)
    existing = _GEOGRAPHIC_HEADING.search(card)
    if existing is not None:
        return _replace_section(card, existing, block)

    return _insert_section(card, _GEOGRAPHIC_HEADING, block, newline)


def update_website_text_section(card: bytes, stats: CardStats) -> bytes:
    """Replace the generated website-text block without adding it to legacy cards."""
    existing = _WEBSITE_TEXT_HEADING.search(card)
    if existing is None:
        return card
    block = _block_bytes(_render_website_text_section(stats), _newline(card))
    return _replace_section(card, existing, block)


def update_language_section(card: bytes, stats: CardStats) -> bytes:
    """Replace the generated language block with canonical population totals."""
    newline = _newline(card)
    existing = _LANGUAGE_HEADING.search(card)
    if existing is not None:
        return _replace_existing_language_section(card, existing, stats, newline)
    if not stats.detected_language_count:
        return card
    return _insert_new_language_section(card, stats, newline)


def update_sentence_section(card: bytes, stats: CardStats) -> bytes:
    """Replace or insert the generated sentence-coverage section."""
    newline = _newline(card)
    block_lines = _render_sentence_section(stats)
    existing = _SENTENCE_HEADING.search(card)
    if existing is not None:
        return _replace_sentence_section(card, existing, block_lines, newline)
    if not block_lines:
        return card
    return _insert_sentence_section(card, block_lines, newline)


def _replace_sentence_section(
    card: bytes, existing: re.Match[bytes], lines: list[str], newline: bytes
) -> bytes:
    """Replace or remove an existing sentence section."""
    return _replace_section(card, existing, _block_bytes(lines, newline) if lines else b"")


def _insert_sentence_section(card: bytes, lines: list[str], newline: bytes) -> bytes:
    """Insert a missing sentence section after the best related section."""
    return _insert_section(card, _SENTENCE_HEADING, _block_bytes(lines, newline), newline)


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
    """Insert a missing generated language section in renderer order."""
    block = _language_section_block(stats, newline)
    return _insert_section(card, _LANGUAGE_HEADING, block, newline)


def _language_section_block(stats: CardStats, newline: bytes) -> bytes:
    """Render one language section using the card's newline convention."""
    return _block_bytes(_render_language_section(stats), newline)


def _newline(card: bytes) -> bytes:
    """Return the card's newline convention, preferring CRLF when present."""
    return b"\r\n" if b"\r\n" in card else b"\n"


def _block_bytes(lines: list[str], newline: bytes) -> bytes:
    """Encode rendered lines as one newline-terminated block."""
    return newline.join(line.encode() for line in lines) + newline


def _insert_section(card: bytes, heading: re.Pattern[bytes], block: bytes, newline: bytes) -> bytes:
    """Insert a missing patched section where ``render_markdown`` would place it."""
    position = _PATCHED_SECTION_ORDER.index(heading)
    for earlier in reversed(_PATCHED_SECTION_ORDER[:position]):
        match = earlier.search(card)
        if match is not None:
            return _insert_after_section(card, match, block)
    for later in _PATCHED_SECTION_ORDER[position + 1 :]:
        match = later.search(card)
        if match is not None:
            return card[: match.start()] + block + card[match.start() :]
    return _append_block(card, block, newline)


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
    return _block_bytes(_render_polygon_geometry_section(geometry), newline)


def _append_block(card: bytes, block: bytes, newline: bytes) -> bytes:
    """Append a block after one blank line while retaining the existing card bytes."""
    prefix = card
    if prefix and not prefix.endswith(newline):
        prefix += newline
    if prefix:
        prefix += newline
    return prefix + block
