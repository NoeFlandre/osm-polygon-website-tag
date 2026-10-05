"""Golden digests pinning every SHA-256 call site to its pre-refactor value."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_website_tag.pipeline.grid5000_bundle import receipt_digest
from osm_polygon_website_tag.pipeline.model_identity import sha256_file as model_sha256_file
from osm_polygon_website_tag.reporting.file_hashing import hash_file
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.digest import canonical_json_sha256, sha256_file

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
