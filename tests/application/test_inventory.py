"""Tests for read-only source and artifact inventory inspection."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.application.inventory import (
    discover_sources,
    source_bundle_is_complete,
    source_inventory_matches_expected,
)
from osm_polygon_website_tag.contracts.comparison_schema import (
    COMPARISON_OBSERVATION_SCHEMA,
)
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.runtime.run_state import (
    SourceFingerprint,
    hash_shard,
    snapshot_source_fingerprint,
)


def test_discover_sources_returns_recursive_relative_path_order(tmp_path: Path) -> None:
    nested = tmp_path / "a"
    nested.mkdir()
    (tmp_path / "z-latest.osm.pbf").write_bytes(b"z")
    (nested / "b-latest.osm.pbf").write_bytes(b"b")

    sources = discover_sources(tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in sources] == [
        "a/b-latest.osm.pbf",
        "z-latest.osm.pbf",
    ]


def test_discover_sources_rejects_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(
        ValueError,
        match=f"^source root is not a directory: {missing.resolve()}$",
    ):
        discover_sources(missing)


def test_discover_sources_rejects_empty_root(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match=f"^no \\.osm\\.pbf files found below source root: {tmp_path.resolve()}$",
    ):
        discover_sources(tmp_path)


def test_discover_sources_reports_sorted_duplicate_basenames(tmp_path: Path) -> None:
    for directory in ("first", "second"):
        child = tmp_path / directory
        child.mkdir()
        (child / "z-latest.osm.pbf").write_bytes(b"z")
        (child / "a-latest.osm.pbf").write_bytes(b"a")

    with pytest.raises(
        ValueError,
        match=(
            "^duplicate source filenames are unsupported: "
            r"\['a-latest\.osm\.pbf', 'z-latest\.osm\.pbf'\]$"
        ),
    ):
        discover_sources(tmp_path)


def _complete_bundle(
    tmp_path: Path,
) -> tuple[Path, SourceFingerprint, dict[str, object], dict[str, Path]]:
    source = tmp_path / "source-latest.osm.pbf"
    source.write_bytes(b"source")
    fingerprint = snapshot_source_fingerprint(source)
    run_dir = tmp_path / "run"
    shard_paths = {
        "public": run_dir / "polygons" / "source-latest.parquet",
        "observation": run_dir / "analysis_observations" / "source-latest.parquet",
        "rejection": run_dir / "rejections" / "source-latest.parquet",
    }
    for path, schema in (
        (shard_paths["public"], POLYGON_PUBLIC_SCHEMA),
        (shard_paths["observation"], COMPARISON_OBSERVATION_SCHEMA),
        (shard_paths["rejection"], REJECTION_SCHEMA),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist([], schema=schema), path)

    manifest: dict[str, object] = {
        **asdict(fingerprint),
        "public_row_count": 0,
        "observation_row_count": 0,
        "rejection_count": 0,
        "public_shard_sha256": hash_shard(shard_paths["public"]),
        "observation_shard_sha256": hash_shard(shard_paths["observation"]),
        "rejection_shard_sha256": hash_shard(shard_paths["rejection"]),
    }
    return run_dir, fingerprint, manifest, shard_paths


def test_source_bundle_is_complete_accepts_exact_artifacts(tmp_path: Path) -> None:
    run_dir, fingerprint, manifest, _paths = _complete_bundle(tmp_path)

    assert source_bundle_is_complete(run_dir, manifest, fingerprint) is True


@pytest.mark.parametrize(
    "failure",
    [
        "missing_manifest",
        "fingerprint",
        "missing_shard",
        "schema",
        "row_count",
        "hash",
    ],
)
def test_source_bundle_is_complete_fails_closed(
    tmp_path: Path,
    failure: str,
) -> None:
    run_dir, fingerprint, manifest, paths = _complete_bundle(tmp_path)
    candidate: dict[str, object] | None = manifest
    if failure == "missing_manifest":
        candidate = None
    elif failure == "fingerprint":
        manifest["size_bytes"] = fingerprint.size_bytes + 1
    elif failure == "missing_shard":
        paths["rejection"].unlink()
    elif failure == "schema":
        pq.write_table(
            pa.table({"unexpected": pa.array([], type=pa.int64())}),
            paths["observation"],
        )
    elif failure == "row_count":
        manifest["public_row_count"] = 1
    elif failure == "hash":
        manifest["public_shard_sha256"] = "0" * 64

    assert source_bundle_is_complete(run_dir, candidate, fingerprint) is False


def test_source_inventory_matches_expected_is_order_independent_for_current_sources() -> None:
    fp_a = SourceFingerprint("a-latest.osm.pbf", size_bytes=1, mtime_ns=2)
    fp_b = SourceFingerprint("b-latest.osm.pbf", size_bytes=3, mtime_ns=4)
    expected = [asdict(fp_a), asdict(fp_b)]

    assert source_inventory_matches_expected(expected, [fp_b, fp_a]) is True


def test_source_inventory_matches_expected_rejects_any_identity_change() -> None:
    fingerprint = SourceFingerprint("a-latest.osm.pbf", size_bytes=1, mtime_ns=2)
    expected = [asdict(fingerprint)]
    changed = SourceFingerprint("a-latest.osm.pbf", size_bytes=1, mtime_ns=3)

    assert source_inventory_matches_expected(expected, [changed]) is False
