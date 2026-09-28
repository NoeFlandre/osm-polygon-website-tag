"""Generated-data root resolution.

The data root holds runs, model caches and Grid'5000 bundles. It comes from
``OSM_POLY_DATA_DIR`` (the environment or ``.env``), else ``./data`` under the
current directory. Immutable PBF sources are supplied explicitly to the CLI
and are never represented as an output data root here.
"""

from __future__ import annotations

from pathlib import Path

from osm_polygon_website_tag.runtime.config import Settings

DATA_ROOT_ENV = "OSM_POLY_DATA_DIR"
DEFAULT_DATA_DIRNAME = "data"


def resolve_data_root() -> Path:
    """Return the absolute generated-data root, without creating it."""
    configured = Settings().osm_poly_data_dir.strip()
    root = Path(configured).expanduser() if configured else Path.cwd() / DEFAULT_DATA_DIRNAME
    return root.resolve()


def data_root_source() -> str:
    """Say whether the root is ``custom`` or the ``default``, never its value."""
    return "custom" if Settings().osm_poly_data_dir.strip() else "default"


def model_cache_dir(name: str) -> Path:
    """Return (and create) the cache directory for one model under the data root."""
    path = resolve_data_root() / "models" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def require_under_data_root(path: Path | str, *, label: str) -> Path:
    """Return ``path`` resolved, refusing one outside the configured data root.

    Runs, models and Grid'5000 bundles all live under that one directory, so a
    path outside it is a mistake.
    """
    normalized = Path(path).expanduser().resolve()
    root = resolve_data_root()
    if not normalized.is_relative_to(root):
        raise ValueError(f"{label} must be under the data root {root} (set {DATA_ROOT_ENV})")
    return normalized


__all__ = [
    "DATA_ROOT_ENV",
    "DEFAULT_DATA_DIRNAME",
    "data_root_source",
    "model_cache_dir",
    "require_under_data_root",
    "resolve_data_root",
]
