"""Tests for bounded, revision-aware artifact hashing."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from osm_polygon_website_tag.reporting.file_hashing import _hash_identified_file, hash_file


def test_hash_file_cache_key_tracks_size_changes(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"one")
    os.utime(path, ns=(10, 10))
    _hash_identified_file.cache_clear()

    assert hash_file(path) == hashlib.sha256(b"one").hexdigest()

    path.write_bytes(b"longer")
    os.utime(path, ns=(10, 10))

    assert hash_file(path) == hashlib.sha256(b"longer").hexdigest()


def test_hash_file_cache_key_tracks_mtime_changes(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"before")
    os.utime(path, ns=(0, 10_000_000_000))
    _hash_identified_file.cache_clear()
    initial_mtime = path.stat().st_mtime_ns

    assert hash_file(path) == hashlib.sha256(b"before").hexdigest()

    path.write_bytes(b"after!")
    os.utime(path, ns=(0, 20_000_000_000))
    assert path.stat().st_mtime_ns != initial_mtime

    assert hash_file(path) == hashlib.sha256(b"after!").hexdigest()
