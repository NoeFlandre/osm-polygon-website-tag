"""Read-only discovery and verification of source and shard inventories."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    is_supported_public_polygon_schema,
    schema_matches,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.runtime.run_state import (
    SourceFingerprint,
    SourceManifestEntry,
    hash_shard,
)
from osm_polygon_website_tag.runtime.safety import normalize_path


def discover_sources(source_root: Path | str) -> list[Path]:
    """Return every PBF below ``source_root`` in deterministic order."""
    root = normalize_path(source_root)
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    sources = sorted(root.rglob("*.osm.pbf"), key=lambda path: path.relative_to(root).as_posix())
    if not sources:
        raise ValueError(f"no .osm.pbf files found below source root: {root}")
    _reject_duplicate_filenames(sources)
    return sources


def _reject_duplicate_filenames(sources: list[Path]) -> None:
    """Reject ambiguous basenames before a source inventory is persisted."""
    name_counts = Counter(source.name for source in sources)
    duplicates = sorted(name for name, count in name_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate source filenames are unsupported: {duplicates}")


def source_inventory_matches_expected(
    expected: list[SourceManifestEntry],
    fingerprints: list[SourceFingerprint],
) -> bool:
    """Return whether current fingerprints exactly match persisted inventory."""
    actual = [asdict(fingerprint) for fingerprint in fingerprints]
    return expected == sorted(actual, key=lambda item: item["filename"])


def source_bundle_is_complete(
    run_dir: Path,
    manifest: SourceManifestEntry | None,
    fingerprint: SourceFingerprint,
) -> bool:
    """Return whether one source's three output shards match their contracts."""
    if manifest is None or not _manifest_matches_fingerprint(manifest, fingerprint):
        return False
    stem = fingerprint.short_id()
    public_path = run_dir / "polygons" / f"{stem}.parquet"
    if not _public_shard_matches(public_path, manifest):
        return False
    return _auxiliary_shards_match(run_dir, stem, manifest)


def _manifest_matches_fingerprint(
    manifest: SourceManifestEntry | None, fingerprint: SourceFingerprint
) -> bool:
    """Check the persisted source identity before opening any shard."""
    return manifest is not None and all(
        manifest.get(key) == value for key, value in asdict(fingerprint).items()
    )


def _public_shard_matches(path: Path, manifest: SourceManifestEntry) -> bool:
    """Validate the public shard schema, row count, and digest."""
    if not path.is_file():
        return False
    parquet = pq.ParquetFile(path)
    return (
        is_supported_public_polygon_schema(parquet.schema_arrow)
        and parquet.metadata.num_rows == manifest.get("public_row_count")
        and hash_shard(path) == manifest.get("public_shard_sha256")
    )


def _auxiliary_shards_match(run_dir: Path, stem: str, manifest: SourceManifestEntry) -> bool:
    """Validate the comparison and rejection shards for one source."""
    paths_and_contracts = (
        (
            run_dir / "analysis_observations" / f"{stem}.parquet",
            COMPARISON_OBSERVATION_SCHEMA,
            "observation_row_count",
            "observation_shard_sha256",
        ),
        (
            run_dir / "rejections" / f"{stem}.parquet",
            REJECTION_SCHEMA,
            "rejection_count",
            "rejection_shard_sha256",
        ),
    )
    for path, schema_contract, count_key, hash_key in paths_and_contracts:
        if not _auxiliary_shard_matches(path, schema_contract, manifest, count_key, hash_key):
            return False
    return True


def _auxiliary_shard_matches(
    path: Path,
    schema_contract: Any,
    manifest: SourceManifestEntry,
    count_key: str,
    hash_key: str,
) -> bool:
    """Validate one non-public shard against its schema and manifest."""
    if not path.is_file():
        return False
    parquet = pq.ParquetFile(path)
    return (
        schema_matches(parquet.schema_arrow, schema_contract)
        and parquet.metadata.num_rows == manifest.get(count_key)
        and hash_shard(path) == manifest.get(hash_key)
    )


__all__ = [
    "discover_sources",
    "source_bundle_is_complete",
    "source_inventory_matches_expected",
]
