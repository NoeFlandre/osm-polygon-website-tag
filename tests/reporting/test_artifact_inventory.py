"""Contracts for the canonical publishable-artifact inventory."""

from __future__ import annotations

import hashlib
from pathlib import Path

from osm_polygon_website_tag.reporting.artifact_inventory import hash_file, publishable_paths


def test_publishable_paths_are_deterministic_and_exclude_operational_files(
    tmp_path: Path,
) -> None:
    files = {
        "polygons/z.parquet": b"z",
        "polygons/a.parquet": b"a",
        "analysis_observations/a.parquet": b"observations",
        "rejections/a.parquet": b"rejections",
        "analysis/cells_global.parquet": b"analysis",
        "manifests/sources.json": b"[]\n",
        "manifests/uploaded_polygons.json": b"operational\n",
        "manifests/completion_receipt.json": b"old receipt\n",
        "README.md": b"card\n",
        "dataset.yaml": b"metadata\n",
        "failures.jsonl": b"failure\n",
        "assets/geographic_polygon_density.png": b"png",
        "analysis/nested/ignored.parquet": b"nested",
        "assets/ignored.png": b"other asset",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    paths = publishable_paths(tmp_path)

    assert tuple(path.relative_to(tmp_path).as_posix() for path in paths) == (
        "README.md",
        "analysis/cells_global.parquet",
        "analysis_observations/a.parquet",
        "assets/geographic_polygon_density.png",
        "dataset.yaml",
        "failures.jsonl",
        "manifests/sources.json",
        "polygons/a.parquet",
        "polygons/z.parquet",
        "rejections/a.parquet",
    )


def test_hash_file_matches_sha256(tmp_path: Path) -> None:
    content = b"artifact contract" * 100_000
    path = tmp_path / "artifact.bin"
    path.write_bytes(content)

    assert hash_file(path) == hashlib.sha256(content).hexdigest()
