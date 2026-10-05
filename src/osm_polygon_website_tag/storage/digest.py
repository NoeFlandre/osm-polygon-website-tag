"""SHA-256 helpers: bounded file hashing and canonical-JSON digests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HASH_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file using bounded reads."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(payload: Any) -> str:
    """Return the SHA-256 hex digest of the compact, key-sorted JSON of ``payload``.

    This is the single canonicalisation shared by every writer and verifier of
    a digest: sorted keys, ``(",", ":")`` separators, default ``ensure_ascii``.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
