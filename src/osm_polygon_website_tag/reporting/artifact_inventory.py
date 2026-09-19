"""Canonical inventory and hashing rules for publishable run artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from osm_polygon_website_tag.reporting import file_hashing as _file_hashing
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.text_population import text_population_manifest_entries
from osm_polygon_website_tag.runtime.run_state import OPERATIONAL_MANIFEST_NAMES

hash_file = _file_hashing.hash_file
_hash_identified_file = _file_hashing._hash_identified_file

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


def data_manifest_sha256(root: Path) -> str:
    """Return the deterministic identity of source manifests and data shards."""
    text_entries = list(text_population_manifest_entries(root))
    if not text_entries:
        return _manifest_sha256(root, _is_data_manifest_path)
    entries = _manifest_entries(root, _is_data_manifest_path)
    entries.extend(
        {
            "path": f"text_population/{entry['path']}",
            "size_bytes": entry["size_bytes"],
            "sha256": entry["sha256"],
        }
        for entry in text_entries
    )
    return _manifest_digest(entries)


def parquet_manifest_sha256(root: Path) -> str:
    """Return the deterministic identity of every published Parquet shard."""
    return _manifest_sha256(root, lambda relative_path: relative_path.endswith(".parquet"))


def _manifest_sha256(root: Path, include: Callable[[str], bool]) -> str:
    """Hash selected publishable paths using their relative path and bytes."""
    canonical = json.dumps(
        _manifest_entries(root, include),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manifest_entries(root: Path, include: Callable[[str], bool]) -> list[dict[str, int | str]]:
    """Return selected publishable identities in deterministic order."""
    return [
        {
            "path": relative_path,
            "size_bytes": path.stat().st_size,
            "sha256": hash_file(path),
        }
        for path in publishable_paths(root)
        if (relative_path := path.relative_to(root).as_posix()) and include(relative_path)
    ]


def _manifest_digest(entries: list[dict[str, int | str]]) -> str:
    """Hash canonical manifest entries."""
    canonical = json.dumps(
        sorted(entries, key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_data_manifest_path(relative_path: str) -> bool:
    """Return whether a published path identifies source or Parquet data."""
    return relative_path in _DATA_MANIFEST_FILES or relative_path.endswith(".parquet")


__all__ = [
    "data_manifest_sha256",
    "hash_file",
    "parquet_manifest_sha256",
    "publishable_paths",
]
