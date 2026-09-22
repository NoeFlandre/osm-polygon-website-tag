"""Focused contracts for private shard verification helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from osm_polygon_website_tag.reporting.verification import shards


def test_shard_helpers_report_metadata_errors_without_network(tmp_path: Path) -> None:
    contract = shards.SHARD_CONTRACTS[0]
    errors: list[str] = []
    checked: list[str] = []
    declared: set[str] = set()
    shards._verify_manifest_entry(
        tmp_path,
        {"filename": "a.osm.pbf", "public_row_count": 0, "public_shard_sha256": "0" * 64},
        errors,
        checked,
        declared,
    )
    assert "a" in declared
    assert any("missing public shard" in error for error in errors)
    errors.clear()
    shards._verify_row_count(1, "a.osm.pbf", contract, {"public_row_count": 0}, errors)
    assert errors
    path = tmp_path / "file"
    path.write_bytes(b"x")
    shards._verify_shard_hash(
        path,
        "a.osm.pbf",
        contract,
        {"public_shard_sha256": hashlib.sha256(b"x").hexdigest()},
        errors,
    )
    assert shards._verify_extra_shards(tmp_path, contract, set(), errors) is None


def test_shard_helpers_cover_valid_and_invalid_inventory_branches(
    tmp_path: Path, monkeypatch
) -> None:
    contract = shards.SHARD_CONTRACTS[0]
    errors: list[str] = []
    checked: list[str] = []
    declared: set[str] = set()
    shards._verify_manifest_entry(
        tmp_path,
        {"filename": "not-a-pbf"},
        errors,
        checked,
        declared,
    )
    assert errors == ["manifest entry has invalid filename"]
    errors.clear()

    shard = tmp_path / contract.directory / "a.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"parquet")
    for other_contract in shards.SHARD_CONTRACTS[1:]:
        other = tmp_path / other_contract.directory / "a.parquet"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"parquet")
    fake_parquet = SimpleNamespace(schema_arrow=object(), metadata=SimpleNamespace(num_rows=2))
    monkeypatch.setattr(shards.pq, "ParquetFile", lambda _path: fake_parquet)
    monkeypatch.setattr(shards, "schema_matches", lambda *_args: True)
    monkeypatch.setattr(shards, "is_current_public_polygon_schema", lambda *_args: True)
    monkeypatch.setattr(shards, "hash_file", lambda _path: "a" * 64)
    entry = {
        "filename": "a.osm.pbf",
        "public_row_count": 2,
        "public_shard_sha256": "a" * 64,
        "observation_row_count": 2,
        "observation_shard_sha256": "a" * 64,
        "rejection_count": 2,
        "rejection_shard_sha256": "a" * 64,
    }
    shards._verify_manifest_entry(tmp_path, entry, errors, checked, declared)
    assert errors == []
    assert declared == {"a"}
    assert checked == ["public:a", "comparison:a", "rejection:a"]

    errors.clear()
    monkeypatch.setattr(shards, "is_current_public_polygon_schema", lambda *_args: False)
    monkeypatch.setattr(shards, "schema_matches", lambda *_args: False)
    shards._verify_shard(tmp_path, "a", "a.osm.pbf", entry, contract, errors)
    assert errors == [f"exact schema mismatch in public shard {shard}"]

    errors.clear()
    shards._verify_row_count(1, "a.osm.pbf", contract, {"public_row_count": True}, errors)
    assert errors == ["invalid public_row_count for a.osm.pbf"]
    errors.clear()
    shards._verify_shard_hash(
        shard,
        "a.osm.pbf",
        contract,
        {"public_shard_sha256": "b" * 64},
        errors,
    )
    assert errors == [f"public shard hash mismatch for a.osm.pbf: {'a' * 64} != {'b' * 64}"]

    errors.clear()
    extra = tmp_path / contract.directory / "extra.parquet"
    extra.write_bytes(b"extra")
    shards._verify_extra_shards(tmp_path, contract, {"a"}, errors)
    assert errors == [f"extra undeclared public shard: {extra}"]


def test_shard_verification_reports_unreadable_and_invalid_hashes(
    tmp_path: Path, monkeypatch
) -> None:
    contract = shards.SHARD_CONTRACTS[0]
    shard = tmp_path / contract.directory / "a.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"bad")
    errors: list[str] = []
    monkeypatch.setattr(
        shards.pq,
        "ParquetFile",
        lambda _path: (_ for _ in ()).throw(RuntimeError("broken parquet")),
    )
    shards._verify_shard(
        tmp_path,
        "a",
        "a.osm.pbf",
        {"public_row_count": 0, "public_shard_sha256": "a" * 64},
        contract,
        errors,
    )
    assert errors == [f"unreadable public shard {shard}: broken parquet"]
    errors.clear()
    shards._verify_shard_hash(
        shard,
        "a.osm.pbf",
        contract,
        {"public_shard_sha256": "short"},
        errors,
    )
    assert errors == ["missing public shard hash for a.osm.pbf"]
