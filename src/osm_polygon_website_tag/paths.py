"""Local data path resolution.

The codebase lives on the local filesystem, but the working dataset is large
and lives on an external drive (``/Volumes/Seagate M3/projects/osm-polygon-website-tag``).
This module centralises that location so nothing else in the codebase hard-codes it.

Override with the ``OSM_POLY_DATA_DIR`` environment variable (see ``.env.example``).
"""

from __future__ import annotations

import os
from pathlib import Path

# External drive used as the default data root. Kept as a string (not Path) so a
# missing drive at import time does not raise; resolution happens lazily.
_DEFAULT_DATA_DIR = "/Volumes/Seagate M3/projects/osm-polygon-website-tag"

# Sub-directory layout under the data root. Add constants here as we grow
# instead of inlining path joins across the codebase.
RAW_DIRNAME = "raw"
PROCESSED_DIRNAME = "processed"
EXPORTS_DIRNAME = "exports"


def data_root() -> Path:
    """Return the local data root, creating it if missing.

    Order of resolution:
      1. ``OSM_POLY_DATA_DIR`` environment variable (if set and non-empty).
      2. ``/Volumes/Seagate M3/projects/osm-polygon-website-tag`` (external drive).
      3. ``./data`` relative to the current working directory (fallback for dev).

    The directory is created on first call so callers can treat it as always-present.
    """
    configured = os.environ.get("OSM_POLY_DATA_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser()
    elif Path(_DEFAULT_DATA_DIR).exists():
        root = Path(_DEFAULT_DATA_DIR)
    else:
        root = Path.cwd() / "data"

    root.mkdir(parents=True, exist_ok=True)
    return root


def raw_dir() -> Path:
    """Directory for raw, immutable OSM extracts (PBF, Overpass dumps)."""
    path = data_root() / RAW_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def processed_dir() -> Path:
    """Directory for cleaned/normalized intermediate artifacts."""
    path = data_root() / PROCESSED_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    """Directory for final artifacts ready to upload to Hugging Face."""
    path = data_root() / EXPORTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path
