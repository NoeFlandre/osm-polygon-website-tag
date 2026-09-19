"""Bounded, cached hashing for release artifacts."""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path


def hash_file(path: Path) -> str:
    """Return the SHA-256 digest of a file using bounded reads."""
    status = path.stat()
    return _hash_identified_file(path, status.st_size, status.st_mtime_ns)


@functools.cache
def _hash_identified_file(path: Path, size: int, mtime_ns: int) -> str:
    """Return the SHA-256 digest of one file revision using bounded reads."""
    del size, mtime_ns
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["hash_file"]
