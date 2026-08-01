"""RED tests for resumable per-source publication decisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm_polygon_website_tag.publishing.incremental import (
    incremental_publish_changed_shard,
    reconcile_upload_checkpoint,
)
from osm_polygon_website_tag.runtime.run_state import hash_shard


def _run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    shard = run_dir / "polygons" / "a.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"shard-a")
    (run_dir / "README.md").write_text("card-a")
    (run_dir / "dataset.yaml").write_text("yaml-a")
    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir()
    map_path.write_bytes(b"\x89PNG\r\n\x1a\nmap-a")
    return run_dir, shard


def test_incremental_uploads_changed_shard_and_bundle(tmp_path: Path, monkeypatch) -> None:
    run_dir, shard = _run(tmp_path)
    captured: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)

    assert plan.shard_changed is True
    assert plan.bundle_changed is True
    assert captured == [
        shard,
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "assets" / "geographic_polygon_density.png",
    ]
    checkpoint = json.loads((run_dir / "manifests" / "uploaded_polygons.json").read_text())
    assert checkpoint["schema_version"] == "v2"
    assert checkpoint["sources"]["a.osm.pbf"]["polygon_sha256"]


def test_incremental_bundle_only_upload_does_not_upload_shard(tmp_path: Path, monkeypatch) -> None:
    run_dir, shard = _run(tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: None,
    )
    incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)
    (run_dir / "README.md").write_text("card-b")
    captured: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)

    assert plan.shard_changed is False
    assert plan.bundle_changed is True
    assert shard not in captured
    assert captured == [
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "assets" / "geographic_polygon_density.png",
    ]


def test_incremental_dry_run_does_not_write_checkpoint_or_upload(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir, _ = _run(tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("uploaded")),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=True)

    assert plan.upload_paths
    assert not (run_dir / "manifests" / "uploaded_polygons.json").exists()


def test_incremental_republishes_bundle_when_map_contract_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir, shard = _run(tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **_kwargs: None,
    )
    incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["global_bundle"]["map_contract_version"] = 0
    checkpoint_path.write_text(json.dumps(checkpoint))

    captured: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)

    assert plan.shard_changed is False
    assert plan.bundle_changed is True
    assert shard not in captured
    assert captured == [
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "assets" / "geographic_polygon_density.png",
    ]


def test_incremental_rejects_malformed_checkpoint(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(
        json.dumps({"schema_version": "v2", "global_bundle": [], "sources": {}})
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"))


def test_reconcile_checkpoint_uses_remote_shards_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, shard = _run(tmp_path)
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "schema_version": "v2",
                "global_bundle": {},
                "sources": {"stale.osm.pbf": {"polygon_sha256": "0" * 64}},
            }
        )
    )
    remote_sha = hash_shard(shard)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental.remote_polygon_shard_hashes",
        lambda **_kwargs: {
            "a.osm.pbf": remote_sha,
            "missing.osm.pbf": "1" * 64,
        },
    )

    checkpoint = reconcile_upload_checkpoint(
        run_dir,
        repo_id="owner/dataset",
        token="token",
    )

    assert checkpoint["sources"] == {"a.osm.pbf": {"polygon_sha256": remote_sha}}
