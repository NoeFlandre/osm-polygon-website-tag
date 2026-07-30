"""Generated-data path resolution.

Code lives on the Mac; generated run artifacts live on the Seagate data
volume. Immutable PBF sources are supplied explicitly to the CLI and are
never represented as an output data root here.

Override with the ``OSM_POLY_DATA_DIR`` environment variable (see ``.env.example``).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATA_ROOT = Path("/Volumes/Seagate M3/projects/osm-polygon-website-tag-data")

# Sub-directory layout under the data root. Add constants here as we grow
# instead of inlining path joins across the codebase.
RAW_DIRNAME = "raw"
PROCESSED_DIRNAME = "processed"
EXPORTS_DIRNAME = "exports"


def data_root() -> Path:
    """Return the generated-data root, creating it if missing.

    Order of resolution:
      1. ``OSM_POLY_DATA_DIR`` environment variable (if set and non-empty).
      2. The dedicated Seagate generated-data directory.

    The directory is created on first call so callers can treat it as always-present.
    """
    configured = os.environ.get("OSM_POLY_DATA_DIR", "").strip()
    root = Path(configured).expanduser() if configured else DEFAULT_DATA_ROOT

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
