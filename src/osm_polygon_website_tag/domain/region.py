"""Region detection from PBF filenames.

The region label is derived from the PBF filename using the Geofabrik
convention: ``<region>-latest.osm.pbf``. The ``-latest`` suffix and the
``.osm.pbf`` (or ``.osm`` or ``.pbf``) extension are stripped; the
remainder is lowercased.
"""

from __future__ import annotations

import re
from pathlib import Path

_STRIP_SUFFIXES: tuple[str, ...] = (".osm.pbf", ".osm", ".pbf")
_STRIP_DATES: tuple[str, ...] = ("-latest",)


def region_from_pbf_filename(filename: str | Path) -> str:
    """Return the lowercased region label extracted from ``filename``."""
    name = Path(str(filename)).name
    name = _strip_suffix(name, _STRIP_SUFFIXES)
    name = _strip_suffix(name, _STRIP_DATES)
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    return cleaned.strip("-") or "unknown"


def _strip_suffix(name: str, suffixes: tuple[str, ...]) -> str:
    """Remove the first matching case-insensitive suffix."""
    lower = name.lower()
    for suffix in suffixes:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name
