"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.application.workflow_helpers import (
    sources as _sources,
)
from tests.fixtures.polygon_shards import project_current_rows_to_legacy

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.application.workflow import (
    discover_sources,
    run_all,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
)
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_COMPLETE,
    STATUS_EXTRACTING,
    hash_shard,
    initialise_run,
    load_run,
    snapshot_source_fingerprint,
    transition_status,
    update_public_shard_metadata,
    upsert_run_metadata,
)

pytestmark = pytest.mark.usefixtures("offline_remote_reconciliation", "static_text_enrichment")


def _track_extractions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record source extractions while delegating to the real implementation."""
    names: list[str] = []
    original_extract = source_processing.extract_pbf

    def track_extract(source, *args, **kwargs):
        names.append(Path(source).name)
        return original_extract(source, *args, **kwargs)

    monkeypatch.setattr(source_processing, "extract_pbf", track_extract)
    return names


def test_run_all_apply_uploads_each_shard_then_complete_run(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    shard_uploads: list[str] = []
    final_uploads: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.resolve_hf_token", lambda: "available"
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing._upload_public_shard",
        lambda _run, source, _repo, *_args: shard_uploads.append(source.name),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.publish_to_hf",
        lambda run_dir, **_kwargs: final_uploads.append(Path(run_dir)),
    )

    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    assert final_uploads == [result.run_dir]
    assert result.uploaded_count == 2


def test_run_all_completes_each_source_before_extracting_the_next(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    root = _sources(make_pbf, tmp_path)
    events: list[str] = []
    original_extract = source_processing.extract_pbf
    original_enrich = source_processing.enrich_polygon_shard

    def track_extract(source, *args, **kwargs):
        events.append(f"extract:{Path(source).name}")
        return original_extract(source, *args, **kwargs)

    def track_enrich(shard, *args, **kwargs):
        events.append(f"enrich:{Path(shard).name}")
        return original_enrich(shard, *args, **kwargs)

    monkeypatch.setattr(source_processing, "extract_pbf", track_extract)
    monkeypatch.setattr(source_processing, "enrich_polygon_shard", track_enrich)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda _run, source, _repo, *_args: events.append(f"upload:{source.name}"),
    )
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    assert events == [
        "extract:a-latest.osm.pbf",
        "enrich:a-latest.parquet",
        "upload:a-latest.osm.pbf",
        "extract:b-latest.osm.pbf",
        "upload:b-latest.osm.pbf",
    ]


def test_run_all_forwards_bounded_worker_configuration(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    extract_settings: list[dict[str, object]] = []
    enrich_settings: list[dict[str, object]] = []
    original_extract = source_processing.extract_pbf
    original_enrich = source_processing.enrich_polygon_shard

    def track_extract(source, run_dir, **kwargs):  # type: ignore[no-untyped-def]
        extract_settings.append(dict(kwargs))
        return original_extract(source, run_dir, **kwargs)

    def track_enrich(shard, **kwargs):  # type: ignore[no-untyped-def]
        enrich_settings.append(dict(kwargs))
        return original_enrich(shard, **kwargs)

    monkeypatch.setattr(source_processing, "extract_pbf", track_extract)
    monkeypatch.setattr(source_processing, "enrich_polygon_shard", track_enrich)

    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        area_workers=3,
        max_in_flight_areas=12,
        fetch_workers=5,
    )

    assert result.complete
    assert [settings["area_workers"] for settings in extract_settings] == [3, 3]
    assert [settings["max_in_flight_areas"] for settings in extract_settings] == [12, 12]
    assert [settings["fetch_workers"] for settings in enrich_settings] == [5]


def test_old_extracting_run_reuses_completed_source_before_continuing(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    root = _sources(make_pbf, tmp_path)
    sources = discover_sources(root)
    fingerprints = [snapshot_source_fingerprint(source) for source in sources]
    output_root = tmp_path / "runs"
    run_dir, state = initialise_run(
        output_root,
        run_id="production",
        expected_sources=fingerprints,
    )
    upsert_run_metadata(state, {"source_root": str(root.resolve())})
    transition_status(state, STATUS_EXTRACTING)
    source_processing.extract_pbf(sources[0], run_dir, run_state=state)

    extracted_on_resume = _track_extractions(monkeypatch)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(source_processing, "_upload_public_shard", lambda *_args: None)
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    result = run_all(
        source_root=root,
        output_root=output_root,
        run_id="production",
        apply=True,
    )

    assert extracted_on_resume == ["b-latest.osm.pbf"]
    assert result.skipped_count == 1
    assert result.extracted_count == 1
    assert result.uploaded_count == 2


def test_resume_after_interruption_before_enrichment_does_not_reextract(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    original_enrich = source_processing.enrich_polygon_shard
    interrupted = False

    def interrupt_first_enrichment(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_enrich(*args, **kwargs)

    monkeypatch.setattr(source_processing, "enrich_polygon_shard", interrupt_first_enrichment)
    with pytest.raises(KeyboardInterrupt):
        run_all(
            source_root=root,
            output_root=tmp_path / "runs",
            run_id="production",
        )

    run_dir = tmp_path / "runs" / "production"
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING
    assert (run_dir / "polygons" / "a-latest.parquet").is_file()

    extracted_on_resume = _track_extractions(monkeypatch)
    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
    )

    assert extracted_on_resume == ["b-latest.osm.pbf"]
    assert result.skipped_count == 1
    assert result.extracted_count == 1
    assert result.complete is True


def test_run_all_refuses_changed_source_inventory(make_pbf, tmp_path: Path) -> None:
    root = _sources(make_pbf, tmp_path)
    result = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    source = next(root.rglob("a-latest.osm.pbf"))
    previous = source.stat()
    os.utime(
        source,
        ns=(previous.st_atime_ns, previous.st_mtime_ns + 20_000_000_000),
    )
    assert source.stat().st_mtime_ns != previous.st_mtime_ns

    with pytest.raises(ValueError, match="inventory changed"):
        run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert result.complete


def test_complete_legacy_run_migrates_without_reextracting_pbf(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    shard = first.run_dir / "polygons" / "a-latest.parquet"
    rows = pq.read_table(shard).to_pylist()
    legacy_rows = project_current_rows_to_legacy(rows, schema_version="v1.1")
    legacy = pa.Table.from_pylist(legacy_rows, schema=POLYGON_PUBLIC_SCHEMA_V1_1)
    pq.write_table(legacy, shard)
    state = load_run(first.run_dir)
    update_public_shard_metadata(
        state,
        filename="a-latest.osm.pbf",
        row_count=legacy.num_rows,
        shard_sha256=hash_shard(shard),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing.extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("legacy migration must not read PBF"),
    )

    resumed = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert resumed.extracted_count == 0
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert load_run(first.run_dir).metadata["status"] == STATUS_COMPLETE


def test_complete_v1_2_run_projects_and_reuploads_without_source_or_web_work(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    root = _sources(make_pbf, tmp_path)
    uploads: list[str] = []
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda _run, source, _repo, *_args: uploads.append(source.name),
    )
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)
    first = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    assert uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    uploads.clear()
    shard = first.run_dir / "polygons" / "a-latest.parquet"
    rows = pq.read_table(shard).to_pylist()
    legacy_rows = project_current_rows_to_legacy(rows, schema_version="v1.2")
    pq.write_table(pa.Table.from_pylist(legacy_rows, schema=POLYGON_PUBLIC_SCHEMA_V1_2), shard)
    state = load_run(first.run_dir)
    update_public_shard_metadata(
        state,
        filename="a-latest.osm.pbf",
        row_count=len(legacy_rows),
        shard_sha256=hash_shard(shard),
    )
    checkpoint_path = first.run_dir / "manifests" / "uploaded_polygons.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["sources"]["a-latest.osm.pbf"]["polygon_sha256"] = hash_shard(shard)
    checkpoint_path.write_text(json.dumps(checkpoint))
    monkeypatch.setattr(
        source_processing,
        "extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("v1.2 migration must not read PBF"),
    )
    monkeypatch.setattr(
        source_processing,
        "enrich_polygon_shard",
        lambda *_args, **_kwargs: pytest.fail("v1.2 migration must not refetch websites"),
    )
    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    assert resumed.extracted_count == 0
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert uploads == ["a-latest.osm.pbf"]


def test_incremental_upload_includes_shard_and_recomputed_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    shard = run_dir / "polygons" / "source.parquet"
    shard.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), shard)
    (run_dir / "README.md").write_text("card")
    (run_dir / "dataset.yaml").write_text("metadata")
    (run_dir / "stats.json").write_text("stats")
    captured: list[Path] = []

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    source_processing._upload_public_shard(run_dir, Path("source.osm.pbf"), "owner/dataset")

    assert captured == [
        shard,
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "stats.json",
    ]
