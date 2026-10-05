"""Bounded, cached hashing for release artifacts."""

from __future__ import annotations

import functools
from pathlib import Path

from osm_polygon_website_tag.storage.digest import sha256_file


def hash_file(path: Path) -> str:
    """Return the SHA-256 digest of a file using bounded reads."""
    status = path.stat()
    return _hash_identified_file(path, status.st_size, status.st_mtime_ns)


@functools.cache
def _hash_identified_file(path: Path, size: int, mtime_ns: int) -> str:
    """Return the SHA-256 digest of one file revision using bounded reads."""
    del size, mtime_ns
    return sha256_file(path)


__all__ = ["hash_file"]
