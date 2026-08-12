"""Tag normalization and presence rules.

Pure functions only. No I/O, no global state.

A tag counts as present when its Unicode-trimmed value is non-empty.

Inclusion keys
--------------

The dataset qualifies polygons via either of these two OSM tag keys:

* ``website``
* ``contact:website``

A polygon qualifies when at least one of those keys is non-empty after
trimming (``has_any_website``). Both original values are preserved
verbatim in the public shard and in the comparison-observation shard;
neither is aliased or overwritten.

The public schema intentionally keeps ``website`` and ``contact_website``
separate. There is no preferred-website projection.

The Wikidata key remains ``wikidata``.
"""

from __future__ import annotations

import re

# Canonical tag keys used in headline analysis.
WEBSITE_KEY = "website"
CONTACT_WEBSITE_KEY = "contact:website"
WIKIDATA_KEY = "wikidata"

# Strip ASCII whitespace as well as common invisible Unicode characters that
# appear in OSM data (non-breaking space, zero-width characters, BOM).
_INVISIBLE_RE = re.compile(
    r"^[\s\u00a0\u200b\u200c\u200d\u2060\ufeff]+|[\s\u00a0\u200b\u200c\u200d\u2060\ufeff]+$"
)


def normalize_value(raw: str | None) -> str:
    """Return the Unicode-trimmed value or ``""`` if it is whitespace-only.

    Trimming removes leading / trailing whitespace, including ``\u00a0``
    (non-breaking space) and ``\u200b`` (zero-width space). After trimming,
    an all-whitespace input becomes ``""``.
    """
    if raw is None:
        return ""
    return _INVISIBLE_RE.sub("", raw)


def is_present(tags: dict[str, str], key: str) -> bool:
    """Return ``True`` iff ``tags[key]`` exists and is non-empty after trimming."""
    value = tags.get(key)
    if value is None:
        return False
    return normalize_value(value) != ""


def has_website(tags: dict[str, str]) -> bool:
    """Return ``True`` iff the polygon has a non-empty ``website`` tag."""
    return is_present(tags, WEBSITE_KEY)


def has_contact_website(tags: dict[str, str]) -> bool:
    """Return ``True`` iff the polygon has a non-empty ``contact:website`` tag.

    Unrelated ``contact:*`` keys (``contact:phone``, ``contact:email`` ...)
    are NOT picked up. Only the exact ``contact:website`` key counts.
    """
    return is_present(tags, CONTACT_WEBSITE_KEY)


def has_any_website(tags: dict[str, str]) -> bool:
    """Return ``True`` iff the polygon qualifies via either website key.

    Inclusion rule:
        has_any_website = has_website OR has_contact_website
    """
    return has_website(tags) or has_contact_website(tags)


def has_wikidata(tags: dict[str, str]) -> bool:
    """Return ``True`` iff the polygon has a non-empty ``wikidata`` tag."""
    return is_present(tags, WIKIDATA_KEY)


__all__ = [
    "CONTACT_WEBSITE_KEY",
    "WEBSITE_KEY",
    "WIKIDATA_KEY",
    "has_any_website",
    "has_contact_website",
    "has_website",
    "has_wikidata",
    "is_present",
    "normalize_value",
]
