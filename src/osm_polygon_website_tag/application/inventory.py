"""Read-only discovery and verification of source and shard inventories."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.runtime.run_state import SourceFingerprint, hash_shard
from osm_polygon_website_tag.runtime.safety import normalize_path


def discover_sources(source_root: Path | str) -> list[Path]:
    """Return every PBF below ``source_root`` in deterministic order."""
    root = normalize_path(source_root)
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    sources = sorted(root.rglob("*.osm.pbf"), key=lambda path: path.relative_to(root).as_posix())
    if not sources:
        raise ValueError(f"no .osm.pbf files found below source root: {root}")
    name_counts = Counter(source.name for source in sources)
    duplicates = sorted(name for name, count in name_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate source filenames are unsupported: {duplicates}")
    return sources


def source_inventory_matches_expected(
    expected: list[dict[str, Any]],
    fingerprints: list[SourceFingerprint],
) -> bool:
    """Return whether current fingerprints exactly match persisted inventory."""
    actual = [asdict(fingerprint) for fingerprint in fingerprints]
    return expected == sorted(actual, key=lambda item: item["filename"])


def source_bundle_is_complete(
    run_dir: Path,
    manifest: dict[str, Any] | None,
    fingerprint: SourceFingerprint,
) -> bool:
    """Return whether one source's three output shards match their contracts."""
    if manifest is None:
        return False
    if any(manifest.get(key) != value for key, value in asdict(fingerprint).items()):
        return False
    stem = fingerprint.short_id()
    paths_and_contracts = (
        (
            run_dir / "polygons" / f"{stem}.parquet",
            (
                POLYGON_PUBLIC_SCHEMA_V1_1,
                POLYGON_PUBLIC_SCHEMA_V1_2,
                POLYGON_PUBLIC_SCHEMA,
            ),
            "public_row_count",
            "public_shard_sha256",
        ),
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
        if not path.is_file():
            return False
        parquet = pq.ParquetFile(path)
        schemas = schema_contract if isinstance(schema_contract, tuple) else (schema_contract,)
        if not any(parquet.schema_arrow.equals(schema, check_metadata=True) for schema in schemas):
            return False
        if parquet.metadata.num_rows != manifest.get(count_key):
            return False
        if hash_shard(path) != manifest.get(hash_key):
            return False
    return True


__all__ = [
    "discover_sources",
    "source_bundle_is_complete",
    "source_inventory_matches_expected",
]
