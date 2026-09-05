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


__all__ = ["ModelIdentity", "sha256_file"]
