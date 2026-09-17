"""Contracts for the canonical publishable-artifact inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from osm_polygon_website_tag.reporting import artifact_inventory
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
        "stats.json": b"{}\n",
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
        "stats.json",
    )


def test_hash_file_matches_sha256(tmp_path: Path) -> None:
    content = b"artifact contract" * 100_000
    path = tmp_path / "artifact.bin"
    path.write_bytes(content)

    assert hash_file(path) == hashlib.sha256(content).hexdigest()


def test_inventory_private_filters_and_manifest_selection_are_exact(tmp_path: Path) -> None:
    for relative in (
        "polygons/a.parquet",
        "polygons/uploaded_polygons.json",
        "polygons/nested/file.parquet",
        "README.md",
        "unknown.txt",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    assert artifact_inventory._directory_publishable_paths(tmp_path / "polygons") == [
        tmp_path / "polygons" / "a.parquet"
    ]
    assert artifact_inventory._root_publishable_paths(tmp_path) == [tmp_path / "README.md"]
    assert artifact_inventory._is_data_manifest_path("manifests/sources.json")
    assert artifact_inventory._is_data_manifest_path("polygons/a.parquet")
    assert not artifact_inventory._is_data_manifest_path("README.md")
    assert not artifact_inventory._is_data_manifest_path("analysis/a.txt")


def test_hash_file_reads_in_bounded_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifact.bin"
    sizes: list[int] = []

    class Stream:
        def __enter__(self) -> Stream:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            sizes.append(size)
            return b"chunk" if len(sizes) == 1 else b""

    monkeypatch.setattr(artifact_inventory.Path, "open", lambda _path, _mode: Stream())
    assert artifact_inventory.hash_file(path) == hashlib.sha256(b"chunk").hexdigest()
    assert sizes == [1024 * 1024, 1024 * 1024]


def test_data_manifest_hash_includes_only_source_manifests_and_parquet_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = []
    for relative, content in (
        ("polygons/z.parquet", b"z"),
        ("manifests/sources.json", b"sources"),
        ("README.md", b"card"),
        ("manifests/expected_sources.json", b"expected"),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        paths.append(path)

    hashed: list[Path] = []

    def fake_hash(path: Path) -> str:
        hashed.append(path)
        return path.name + "-digest"

    monkeypatch.setattr(artifact_inventory, "publishable_paths", lambda _root: tuple(paths))
    monkeypatch.setattr(artifact_inventory, "hash_file", fake_hash)
    actual = artifact_inventory.data_manifest_sha256(tmp_path)
    selected = [
        {
            "path": path.relative_to(tmp_path).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": path.name + "-digest",
        }
        for path in paths
        if artifact_inventory._is_data_manifest_path(path.relative_to(tmp_path).as_posix())
    ]
    expected = hashlib.sha256(
        json.dumps(selected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert actual == expected
    assert hashed == [
        tmp_path / "polygons/z.parquet",
        tmp_path / "manifests/sources.json",
        tmp_path / "manifests/expected_sources.json",
    ]
