"""Validation of per-source shard inventory and shard metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    is_current_public_polygon_schema,
    schema_matches,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.reporting.artifact_inventory import hash_file
from osm_polygon_website_tag.runtime.run_state import SourceManifestEntry


@dataclass(frozen=True)
class ShardContract:
    """Complete verification contract for one per-source shard."""

    kind: str
    directory: str
    count_key: str
    hash_key: str
    schema: pa.Schema


SHARD_CONTRACTS: tuple[ShardContract, ...] = (
    ShardContract(
        kind="public",
        directory="polygons",
        count_key="public_row_count",
        hash_key="public_shard_sha256",
        schema=POLYGON_PUBLIC_SCHEMA,
    ),
    ShardContract(
        kind="comparison",
        directory="analysis_observations",
        count_key="observation_row_count",
        hash_key="observation_shard_sha256",
        schema=COMPARISON_OBSERVATION_SCHEMA,
    ),
    ShardContract(
        kind="rejection",
        directory="rejections",
        count_key="rejection_count",
        hash_key="rejection_shard_sha256",
        schema=REJECTION_SCHEMA,
    ),
)


def verify_shards(
    root: Path,
    manifest: list[SourceManifestEntry],
    errors: list[str],
    checked: list[str],
) -> set[str]:
    """Verify declared and on-disk shards, returning declared source stems."""
    declared: set[str] = set()
    for entry in manifest:
        _verify_manifest_entry(root, entry, errors, checked, declared)
    for contract in SHARD_CONTRACTS:
        _verify_extra_shards(root, contract, declared, errors)
    return declared


def _verify_manifest_entry(
    root: Path,
    entry: Mapping[str, object],
    errors: list[str],
    checked: list[str],
    declared: set[str],
) -> None:
    filename = entry.get("filename")
    if not isinstance(filename, str) or not filename.endswith(".osm.pbf"):
        errors.append("manifest entry has invalid filename")
        return
    stem = filename.removesuffix(".osm.pbf")
    declared.add(stem)
    for contract in SHARD_CONTRACTS:
        checked.append(f"{contract.kind}:{stem}")
        _verify_shard(root, stem, filename, entry, contract, errors)


def _verify_shard(
    root: Path,
    stem: str,
    filename: str,
    entry: Mapping[str, object],
    contract: ShardContract,
    errors: list[str],
) -> None:
    path = root / contract.directory / f"{stem}.parquet"
    if not path.is_file():
        errors.append(f"missing {contract.kind} shard: {path}")
        return
    try:
        parquet = pq.ParquetFile(path)
        actual_schema = parquet.schema_arrow
        actual_count = int(parquet.metadata.num_rows)
    except Exception as exc:
        errors.append(f"unreadable {contract.kind} shard {path}: {exc}")
        return
    if not _schema_matches_contract(actual_schema, contract):
        errors.append(f"exact schema mismatch in {contract.kind} shard {path}")
    _verify_row_count(actual_count, filename, contract, entry, errors)
    _verify_shard_hash(path, filename, contract, entry, errors)


def _schema_matches_contract(actual: pa.Schema, contract: ShardContract) -> bool:
    """Accept both current public schemas while keeping other contracts exact."""
    if contract.kind == "public":
        return is_current_public_polygon_schema(actual)
    return schema_matches(actual, contract.schema)


def _verify_row_count(
    actual_count: int,
    filename: str,
    contract: ShardContract,
    entry: Mapping[str, object],
    errors: list[str],
) -> None:
    expected_count = entry.get(contract.count_key)
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        errors.append(f"invalid {contract.count_key} for {filename}")
    elif actual_count != expected_count:
        errors.append(
            f"{contract.kind} row count mismatch for {filename}: "
            f"manifest={expected_count}, parquet={actual_count}"
        )


def _verify_shard_hash(
    path: Path,
    filename: str,
    contract: ShardContract,
    entry: Mapping[str, object],
    errors: list[str],
) -> None:
    expected_hash = entry.get(contract.hash_key)
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        errors.append(f"missing {contract.kind} shard hash for {filename}")
        return
    actual_hash = hash_file(path)
    if actual_hash != expected_hash:
        errors.append(
            f"{contract.kind} shard hash mismatch for {filename}: {actual_hash} != {expected_hash}"
        )


def _verify_extra_shards(
    root: Path,
    contract: ShardContract,
    declared: set[str],
    errors: list[str],
) -> None:
    shard_dir = root / contract.directory
    if not shard_dir.is_dir():
        errors.append(f"missing shard directory: {shard_dir}")
        return
    for path in sorted(shard_dir.glob("*.parquet")):
        if path.stem not in declared:
            errors.append(f"extra undeclared {contract.kind} shard: {path}")
