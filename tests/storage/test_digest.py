"""Golden digests pinning every SHA-256 call site to its pre-refactor value."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from osm_polygon_website_tag.pipeline.grid5000_bundle import receipt_digest
from osm_polygon_website_tag.pipeline.model_identity import sha256_file as model_sha256_file
from osm_polygon_website_tag.reporting.file_hashing import hash_file
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.digest import (
    HASH_CHUNK_BYTES,
    canonical_json_sha256,
    sha256_file,
)

PAYLOAD = [
    {"path": "é.parquet", "size_bytes": 3, "sha256": "ab"},
    {"path": "a", "size_bytes": 1, "sha256": "cd"},
]
PAYLOAD_DIGEST = "6d3ca406d125707ac3522f7b17c9e81e842a1959c6bfe87076bf1ac5df89171f"
# 1_500_000 bytes: spans more than one read chunk.
FILE_DIGEST = "796374667fc0edbeb8a1455bdef32ba3e48763e21aacc2660ceb12787d1910f0"


def test_canonical_json_sha256_golden() -> None:
    assert canonical_json_sha256(PAYLOAD) == PAYLOAD_DIGEST


def test_receipt_digest_is_truncated_canonical_digest() -> None:
    assert receipt_digest({"a": PAYLOAD}) == canonical_json_sha256({"a": PAYLOAD})[:16]
    assert receipt_digest({"b": 1, "a": 2}) == receipt_digest({"a": 2, "b": 1})


def test_every_file_hasher_matches_golden(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"hello" * 300000)
    assert sha256_file(path) == FILE_DIGEST
    assert hash_shard(path) == FILE_DIGEST
    assert hash_file(path) == FILE_DIGEST
    assert model_sha256_file(path) == FILE_DIGEST


class _SpyHandle:
    """File stand-in that records read sizes and fails fast if read past EOF twice."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.sizes: list[int] = []
        self._chunks = iter(chunks)

    def __enter__(self) -> _SpyHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.sizes.append(size)
        if len(self.sizes) > 10:
            raise AssertionError("read loop was not stopped by the empty chunk")
        return next(self._chunks, b"")


def test_sha256_file_reads_bounded_chunks_until_first_empty_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _SpyHandle([b"ab", b"cd", b""])
    monkeypatch.setattr(Path, "open", lambda _path, mode: handle if mode == "rb" else None)

    assert sha256_file(tmp_path / "x") == hashlib.sha256(b"abcd").hexdigest()
    assert handle.sizes == [HASH_CHUNK_BYTES] * 3


def test_sha256_file_empty_and_exact_chunk_multiple(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    exact = tmp_path / "exact"
    exact.write_bytes(b"x" * (2 * HASH_CHUNK_BYTES))

    assert sha256_file(empty) == hashlib.sha256(b"").hexdigest()
    assert sha256_file(exact) == hashlib.sha256(b"x" * (2 * HASH_CHUNK_BYTES)).hexdigest()
