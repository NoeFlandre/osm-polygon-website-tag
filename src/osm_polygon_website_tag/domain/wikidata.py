"""Wikidata value classification and QID extraction.

Pure functions only. No I/O, no global state.

Contract: the caller must pass a non-empty ``value`` to
:class:`classify_wikidata` and :func:`extract_qid`. An absent Wikidata tag
is filtered out by :mod:`osm_polygon_website_tag.domain.tags` before any object
is considered for inclusion in the public dataset, so an empty value
should never reach these functions.

Classes:
    - ``canonical_qid``  a single QID matching ``^Q\\d+$`` (case-insensitive)
    - ``multiple``        several values separated by ``;``, ``,``,
                          whitespace, or newline
    - ``malformed``       the value is non-empty but not a canonical QID
"""

from __future__ import annotations

import re
from enum import StrEnum


class WikidataClass(StrEnum):
    """Discrete Wikidata-value classes."""

    CANONICAL_QID = "canonical_qid"
    MULTIPLE = "multiple"
    MALFORMED = "malformed"


_SEP_RE = re.compile(r"[;,\s]+")
_QID_RE = re.compile(r"^[Qq]\d+$")


def classify_wikidata(value: str) -> WikidataClass:
    """Classify a non-empty ``value`` into one of the :class:`WikidataClass` kinds.

    The caller must guarantee ``value`` is non-empty after trimming.
    """
    stripped = value.strip()
    if not stripped:
        return WikidataClass.MALFORMED

    parts = [p for p in _SEP_RE.split(stripped) if p]
    if len(parts) > 1:
        return WikidataClass.MULTIPLE

    if _QID_RE.match(stripped):
        return WikidataClass.CANONICAL_QID

    return WikidataClass.MALFORMED


def extract_qid(value: str) -> str | None:
    """Return the first canonical QID (uppercased) in a non-empty ``value``.

    The caller must guarantee ``value`` is non-empty after trimming.
    """
    stripped = value.strip()
    if not stripped:
        return None

    for part in _SEP_RE.split(stripped):
        if _QID_RE.match(part):
            return part.upper()
    return None
