"""Generated-data path resolution.

Code lives on the Mac; generated run artifacts live on the Seagate project
volume. Immutable PBF sources are supplied explicitly to the CLI and are
never represented as an output data root here.

Override with the ``OSM_POLY_DATA_DIR`` environment variable (see ``.env.example``).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATA_ROOT = Path("/Volumes/Seagate M3/projects/osm-polygon-website-tag")
LEGACY_DATA_ROOT = Path("/Volumes/Seagate M3/projects/osm-polygon-website-tag-data")

# Sub-directory layout under the data root. Add constants here as we grow
# instead of inlining path joins across the codebase.
RAW_DIRNAME = "raw"
PROCESSED_DIRNAME = "processed"
EXPORTS_DIRNAME = "exports"


def data_root() -> Path:
    """Return the generated-data root, creating it if missing.

    Order of resolution:
      1. ``OSM_POLY_DATA_DIR`` environment variable (if set and non-empty).
      2. The canonical Seagate project directory.

    The directory is created on first call so callers can treat it as always-present.
    """
    root = _configured_data_root()
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


def glotlid_model_cache_dir() -> Path:
    """Return the generated-data directory reserved for the GlotLID model cache."""
    normalized_root = _configured_data_root().resolve()
    if not _is_under_seagate_root(normalized_root):
        raise ValueError(
            f"GlotLID model cache must be under a Seagate data root: {DEFAULT_DATA_ROOT}"
        )
    path = normalized_root / "models" / "glotlid"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assert_seagate_path(path: Path | str, *, label: str) -> Path:
    """Require a production path to be inside an approved Seagate data root.

    The canonical root receives new output. The legacy root remains approved so
    callers can resume or inspect runs created before the storage-root change.
    """
    normalized = Path(path).expanduser().resolve()
    if not _is_under_seagate_root(normalized):
        raise ValueError(f"{label} must be under a Seagate data root: {DEFAULT_DATA_ROOT}")
    return normalized


def _configured_data_root() -> Path:
    configured = os.environ.get("OSM_POLY_DATA_DIR", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DATA_ROOT


def _is_under_seagate_root(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in _seagate_roots())


def _seagate_roots() -> tuple[Path, ...]:
    return tuple(root.resolve() for root in (DEFAULT_DATA_ROOT, LEGACY_DATA_ROOT))
