"""Canonical inventory and hashing rules for publishable run artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.runtime.run_state import OPERATIONAL_MANIFEST_NAMES

_PUBLISHABLE_DIRECTORIES = (
    "polygons",
    "analysis_observations",
    "rejections",
    "analysis",
    "manifests",
)
_PUBLISHABLE_ROOT_FILES = ("README.md", "dataset.yaml", "failures.jsonl")


def publishable_paths(root: Path) -> tuple[Path, ...]:
    """Return the deterministic, content-only publication inventory."""
    paths = [
        path
        for directory in _PUBLISHABLE_DIRECTORIES
        for path in _directory_publishable_paths(root / directory)
    ]
    paths.extend(_root_publishable_paths(root))
    map_path = root / POLYGON_DENSITY_ASSET_REL_PATH
    if map_path.is_file():
        paths.append(map_path)
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def _directory_publishable_paths(directory: Path) -> list[Path]:
    """Return files from one managed directory, excluding operational state."""
    return [
        path
        for path in directory.glob("*")
        if path.is_file() and path.name not in OPERATIONAL_MANIFEST_NAMES
    ]


def _root_publishable_paths(root: Path) -> list[Path]:
    """Return public root metadata files that exist."""
    return [path for name in _PUBLISHABLE_ROOT_FILES if (path := root / name).is_file()]


def hash_file(path: Path) -> str:
    """Return the SHA-256 digest of a file using bounded reads."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["hash_file", "publishable_paths"]
