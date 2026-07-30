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
    lower = name.lower()
    for suffix in _STRIP_SUFFIXES:
        if lower.endswith(suffix):
            name = name[: -len(suffix)]
            break
    for date_suffix in _STRIP_DATES:
        if name.lower().endswith(date_suffix):
            name = name[: -len(date_suffix)]
            break
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    return cleaned.strip("-") or "unknown"
