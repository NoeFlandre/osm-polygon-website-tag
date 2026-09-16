"""Canonical inventory and hashing rules for publishable run artifacts."""

from __future__ import annotations

import hashlib
import json
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
_PUBLISHABLE_ROOT_FILES = ("README.md", "dataset.yaml", "failures.jsonl", "stats.json")
_DATA_MANIFEST_FILES = frozenset(
    {
        "manifests/expected_sources.json",
        "manifests/sources.json",
    }
)


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


def data_manifest_sha256(root: Path) -> str:
    """Return the deterministic identity of source manifests and data shards."""
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": hash_file(path),
        }
        for path in publishable_paths(root)
        if _is_data_manifest_path(path.relative_to(root).as_posix())
    ]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_data_manifest_path(relative_path: str) -> bool:
    """Return whether a published path identifies source or Parquet data."""
    return relative_path in _DATA_MANIFEST_FILES or relative_path.endswith(".parquet")


__all__ = ["data_manifest_sha256", "hash_file", "publishable_paths"]
