"""Generated-data path resolution.

Everything lives on the Seagate project volume: the checkout sits in ``repo/``
beside the runs, models and Grid'5000 bundles it produces. Immutable PBF
sources are supplied explicitly to the CLI and are never represented as an
output data root here.

Override with the ``OSM_POLY_DATA_DIR`` environment variable (see ``.env.example``).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATA_ROOT = Path("/Volumes/Seagate M3/projects/osm-polygon-website-tag")

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
    return _data_subdirectory(RAW_DIRNAME)


def processed_dir() -> Path:
    """Directory for cleaned/normalized intermediate artifacts."""
    return _data_subdirectory(PROCESSED_DIRNAME)


def exports_dir() -> Path:
    """Directory for final artifacts ready to upload to Hugging Face."""
    return _data_subdirectory(EXPORTS_DIRNAME)


def _data_subdirectory(name: str) -> Path:
    """Create and return one directory directly under the data root.

    ``data_root`` has already created the parent, so this needs no recursive
    creation of its own.
    """
    path = data_root() / name
    path.mkdir(exist_ok=True)
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


def sat_model_cache_dir() -> Path:
    """Return the generated-data directory reserved for the SaT model cache."""
    normalized_root = _configured_data_root().resolve()
    if not _is_under_seagate_root(normalized_root):
        raise ValueError(f"SaT model cache must be under a Seagate data root: {DEFAULT_DATA_ROOT}")
    path = normalized_root / "models" / "sat"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assert_seagate_path(path: Path | str, *, label: str) -> Path:
    """Require a production path to be inside the Seagate project root.

    Runs, models and Grid'5000 bundles all live under that one directory, so a
    path outside it is a mistake rather than an older layout.
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
    return (DEFAULT_DATA_ROOT.resolve(),)
