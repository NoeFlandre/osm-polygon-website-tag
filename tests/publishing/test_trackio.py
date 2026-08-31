"""Tests for receipt-bound Trackio metric publishing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import osm_polygon_website_tag.publishing.trackio as trackio_module
from osm_polygon_website_tag.reporting.card_stats import CardStats


def _complete_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    manifests = run_dir / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "run.json").write_text(json.dumps({"status": "complete"}))
    (manifests / "completion_receipt.json").write_text(json.dumps({"manifest_digest": "a" * 64}))
    return run_dir


def test_build_snapshot_projects_card_stats_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _complete_run(tmp_path)
    verification_roots: list[Path] = []

    def verify(root: Path) -> SimpleNamespace:
        verification_roots.append(root)
        return SimpleNamespace(ok=True, errors=[])

    monkeypatch.setattr(
        trackio_module,
        "verify_results",
        verify,
    )
    monkeypatch.setattr(
        trackio_module,
        "compute_card_stats",
        lambda _root: CardStats(
            public_row_count=10,
            sources_count=2,
            expected_sources_count=3,
            observation_count=12,
            rejection_count=4,
            duplicate_count=1,
            conflicting_snapshot_count=2,
            enriched_sources_count=1,
            website_urls_present=8,
            website_text_success_count=4,
            website_total_words=40,
            contact_website_urls_present=2,
            contact_website_text_success_count=1,
            contact_website_total_words=10,
            polygons_with_any_text=5,
            occupied_h3_cell_count=3,
            polygon_density_row_count=5,
        ),
    )

    snapshot = trackio_module.build_trackio_snapshot(run_dir)

    assert verification_roots == [run_dir]
    assert snapshot.run_name == "dataset-" + "a" * 16
    assert snapshot.manifest_digest == "a" * 64
    assert snapshot.config == {
        "dataset_repo": "NoeFlandre/osm-polygon-website-tag",
        "dataset_snapshot": "a" * 64,
        "metric_source": "completion_receipt_and_public_parquets",
    }
    assert snapshot.metrics["dataset_public_polygon_rows"] == 10
    assert set(snapshot.metrics) == {
        "contact_website_text_coverage",
        "dataset_public_polygon_rows",
        "dataset_source_shards",
        "occupied_h3_cells",
        "polygons_with_extracted_text",
        "total_extracted_words",
        "website_text_coverage",
    }
    assert snapshot.metrics["website_text_coverage"] == 0.5
    assert snapshot.metrics["contact_website_text_coverage"] == 0.5
    assert snapshot.metrics["total_extracted_words"] == 50


def test_snapshot_config_can_identify_its_artifact_source() -> None:
    snapshot = trackio_module.TrackioSnapshot(
        run_name="dataset-canonical",
        manifest_digest="b" * 64,
        dataset_repo="owner/dataset",
        metrics={},
        metric_source="canonical_public_manifest_and_card",
    )

    assert snapshot.config["metric_source"] == "canonical_public_manifest_and_card"


def test_build_snapshot_requires_complete_receipt_bound_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    manifests = run_dir / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "run.json").write_text(json.dumps({"status": "enriched"}))

    with pytest.raises(ValueError, match="complete status"):
        trackio_module.build_trackio_snapshot(run_dir)


def test_build_snapshot_rejects_invalid_receipt_digest(tmp_path: Path) -> None:
    run_dir = _complete_run(tmp_path)
    (run_dir / "manifests" / "completion_receipt.json").write_text(
        json.dumps({"manifest_digest": "not-a-sha"})
    )

    with pytest.raises(ValueError, match="invalid manifest digest"):
        trackio_module.build_trackio_snapshot(run_dir)


def test_build_snapshot_rejects_unverified_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _complete_run(tmp_path)
    monkeypatch.setattr(
        trackio_module,
        "verify_results",
        lambda _root: SimpleNamespace(ok=False, errors=["tampered card"]),
    )

    with pytest.raises(ValueError, match="verified run: tampered card"):
        trackio_module.build_trackio_snapshot(run_dir)


def test_publish_snapshot_uses_public_resumable_trackio_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def init(**kwargs: object) -> SimpleNamespace:
        calls["init"] = kwargs
        return SimpleNamespace(id="run-123")

    def log(metrics: dict[str, int | float]) -> None:
        calls["metrics"] = metrics

    def finish() -> None:
        calls["finished"] = True

    def sync(**kwargs: object) -> str:
        calls["sync"] = kwargs
        return "owner/metrics"

    monkeypatch.setitem(
        sys.modules,
        "trackio",
        SimpleNamespace(init=init, log=log, finish=finish, sync=sync),
    )
    snapshot = trackio_module.TrackioSnapshot(
        run_name="dataset-abc",
        manifest_digest="a" * 64,
        dataset_repo="owner/dataset",
        metrics={"dataset_public_polygon_rows": 3},
    )

    result = trackio_module.publish_trackio_snapshot(
        snapshot,
        space_id="owner/metrics",
        project="dataset-project",
    )

    assert result == {
        "space_id": "owner/metrics",
        "project": "dataset-project",
        "run_name": "dataset-abc",
        "run_id": "run-123",
    }
    init_kwargs = cast(dict[str, object], calls["init"])
    assert "space_id" not in init_kwargs
    assert init_kwargs["private"] is False
    assert init_kwargs["resume"] == "allow"
    assert calls["metrics"] == {"dataset_public_polygon_rows": 3}
    assert calls["finished"] is True
    sync_kwargs = cast(dict[str, object], calls["sync"])
    assert sync_kwargs == {
        "project": "dataset-project",
        "space_id": "owner/metrics",
        "private": False,
        "sdk": "static",
    }
