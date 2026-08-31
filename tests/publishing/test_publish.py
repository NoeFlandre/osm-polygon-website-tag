"""Tests for publish.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.publishing.publish import (
    PublishPlan,
    _upload_folder,
    build_publish_plan,
    create_repo,
    publish_to_hf,
)
from osm_polygon_website_tag.runtime.run_state import (
    hash_shard,
    initialise_run,
    record_processed_source,
    snapshot_source_fingerprint,
)


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row(polygon_id: str = "p1"):
    return {
        "polygon_id": polygon_id,
        "region": "monaco",
        "source_pbf": "monaco-latest.osm.pbf",
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "website": "https://example.com",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "preferred_website": "https://example.com",
        "preferred_website_source": "website",
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.com",
        "contact_website_hostname": None,
        "wikidata": "Q42",
        "wikidata_qid": "Q42",
        "wikidata_class": "canonical_qid",
        "name": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": "building",
        "geometry": json.dumps({"type": "Polygon", "coordinates": []}),
        "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0.0,0.0,0.0,0.0]",
        "area_m2": 50.0,
        "area_km2": 5e-5,
        "area_bucket": "10-100m2",
        "centroid_kind": "lambert_azimuthal_equal_area",
        "schema_version": "v1.2",
        "website_text": "example text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _setup_run(tmp_path: Path) -> Path:
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pub.parent.mkdir(parents=True, exist_ok=True)
    shard = pq.write_table(  # noqa: F841
        pa.Table.from_pylist([_row()], schema=POLYGON_PUBLIC_SCHEMA), pub, compression="snappy"
    )
    obs = run_dir / "analysis_observations" / "monaco-latest.parquet"
    obs.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA),
        obs,
        compression="snappy",
    )
    rej = run_dir / "rejections" / "monaco-latest.parquet"
    rej.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([], schema=REJECTION_SCHEMA), rej, compression="snappy")
    record_processed_source(
        state,
        fp,
        public_row_count=1,
        observation_row_count=0,
        rejection_count=0,
        public_shard_sha256=hash_shard(pub),
        observation_shard_sha256=hash_shard(obs),
        rejection_shard_sha256=hash_shard(rej),
    )
    return run_dir


def test_build_publish_plan_lists_artifacts(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    plan = build_publish_plan(run_dir)
    assert isinstance(plan, PublishPlan)
    assert any("polygons" in str(p) for p in plan.artifact_paths)
    assert any("analysis_observations" in str(p) for p in plan.artifact_paths)
    assert any("rejections" in str(p) for p in plan.artifact_paths)


def test_build_publish_plan_excludes_staging(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    staging = run_dir / "staging"
    staging.mkdir()
    (staging / "temp.tmp").write_text("data")
    plan = build_publish_plan(run_dir)
    assert not any("staging" in str(p) for p in plan.artifact_paths)


def test_build_publish_plan_includes_readme(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    (run_dir / "README.md").write_text("# Test\n")
    plan = build_publish_plan(run_dir)
    assert plan.readme_path is not None


def test_build_publish_plan_binds_completed_receipt_artifacts(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    receipt = run_dir / "manifests" / "completion_receipt.json"
    receipt.write_text(
        json.dumps({"artifacts": [{"path": "polygons/monaco-latest.parquet"}]}),
        encoding="utf-8",
    )
    (run_dir / "README.md").write_text("# Test\n", encoding="utf-8")

    plan = build_publish_plan(run_dir)

    assert plan.artifact_paths == [
        run_dir / "polygons" / "monaco-latest.parquet",
        receipt,
    ]
    assert plan.readme_path == run_dir / "README.md"


def test_publish_to_hf_dry_run_skips_network(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    plan = publish_to_hf(run_dir, dry_run=True)
    assert plan.repo_id == "NoeFlandre/osm-polygon-website-tag"


def test_publish_to_hf_refuses_on_verification_failure(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    # Corrupt the shard.
    (run_dir / "polygons" / "monaco-latest.parquet").write_bytes(b"junk")
    import pytest

    with pytest.raises(ValueError):
        publish_to_hf(run_dir, dry_run=False)


def test_publish_to_hf_requires_token_when_not_dry_run(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        publish_to_hf(run_dir, dry_run=False)


def test_create_repo_requires_token(monkeypatch) -> None:
    import pytest

    monkeypatch.setattr("osm_polygon_website_tag.publishing.publish.resolve_hf_token", lambda: None)
    with pytest.raises(ValueError):
        create_repo(repo_id="foo/bar")


def test_create_repo_calls_hf_api(tmp_path: Path, monkeypatch) -> None:
    called: dict[str, object] = {}

    def fake_create_repo_remote(*, repo_id, repo_kind, exist_ok):
        called["repo_id"] = repo_id
        called["repo_kind"] = repo_kind
        called["exist_ok"] = exist_ok
        return repo_id

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.publish._create_repo_remote", fake_create_repo_remote
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.publish.resolve_hf_token", lambda: "abc"
    )
    repo = create_repo(repo_id="foo/bar")
    assert repo == "foo/bar"
    assert called["repo_id"] == "foo/bar"


def test_upload_folder_targets_repository_root(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_upload_folder(**kwargs):
        captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(upload_large_folder=fake_upload_folder),
    )

    _upload_folder(
        tmp_path,
        repo_id="owner/dataset",
        repo_kind="dataset",
        artifact_paths=[],
    )

    assert captured["folder_path"] == str(tmp_path)
    assert "path_in_repo" not in captured
    assert captured["allow_patterns"] == []


def test_upload_folder_converts_receipt_paths_to_repository_patterns(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_upload_folder(**kwargs):
        captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(upload_large_folder=fake_upload_folder),
    )
    readme = tmp_path / "README.md"
    shard = tmp_path / "polygons" / "a.parquet"

    _upload_folder(
        tmp_path,
        repo_id="owner/dataset",
        repo_kind="dataset",
        artifact_paths=[readme, shard],
    )

    assert captured["repo_id"] == "owner/dataset"
    assert captured["repo_type"] == "dataset"
    assert captured["folder_path"] == str(tmp_path)
    assert captured["allow_patterns"] == ["README.md", "polygons/a.parquet"]


def test_create_repo_remote_requests_public_dataset(monkeypatch) -> None:
    from osm_polygon_website_tag.publishing.publish import _create_repo_remote

    captured: dict[str, object] = {}

    def fake_create_repo(**kwargs):
        captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(create_repo=fake_create_repo),
    )

    _create_repo_remote(repo_id="owner/dataset", repo_kind="dataset", exist_ok=True)

    assert captured == {
        "repo_id": "owner/dataset",
        "repo_type": "dataset",
        "exist_ok": True,
        "private": False,
    }
