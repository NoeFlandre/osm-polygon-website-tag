"""Pinned identity shared by every model-backed pipeline stage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ModelIdentity:
    """Immutable identity of the model artifact used for one run."""

    repository: str
    filename: str
    revision: str
    sha256: str


def sha256_file(path: Path) -> str:
    """Hash a model artifact in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(directory: Path) -> str:
    """Hash every file in a model directory by relative path and content.

    Names are folded into the digest alongside contents so that a reshuffled
    repository is recognized as a different model rather than the same one.
    """
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    digest = hashlib.sha256()
    for path in sorted(child for child in directory.rglob("*") if child.is_file()):
        # Both updates are fixed-length hex, so no separator byte is needed to
        # keep the name and content boundaries unambiguous.
        name = path.relative_to(directory).as_posix()
        digest.update(hashlib.sha256(name.encode()).hexdigest().encode())
        digest.update(sha256_file(path).encode())
    return digest.hexdigest()


__all__ = ["ModelIdentity", "sha256_directory", "sha256_file"]
