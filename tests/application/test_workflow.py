"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import project_current_rows_to_legacy

from osm_polygon_website_tag.application.inventory import (
    discover_sources as inventory_discover_sources,
)
from osm_polygon_website_tag.application.workflow import (
    _upload_public_shard,
    discover_sources,
    run_all,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
)
from osm_polygon_website_tag.contracts.text_schema import count_words
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
from osm_polygon_website_tag.web.text_extract import TextExtraction
from osm_polygon_website_tag.web.web_fetch import FetchResult

_EMPTY_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/></osm>
"""

_WEBSITE_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/><tag k="contact:website" v="example.org"/>
  </way>
</osm>
"""


@pytest.fixture(autouse=True)
def _offline_remote_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep workflow tests local; remote reconciliation has dedicated unit tests."""
    from osm_polygon_website_tag.publishing.incremental import load_upload_checkpoint

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.reconcile_upload_checkpoint",
        lambda run_dir, **_kwargs: load_upload_checkpoint(run_dir),
    )


def test_workflow_preserves_discover_sources_compatibility_import() -> None:
    assert discover_sources is inventory_discover_sources
    from osm_polygon_website_tag.application import resume_planner, workflow

    assert workflow.prioritize_sources is resume_planner.prioritize_sources


def test_shard_needs_enrichment_scans_status_columns_without_row_dicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume checks should inspect Arrow columns, not materialize every row."""
    from osm_polygon_website_tag.application import workflow

    class FakeBatch:
        def column(self, name: str) -> pa.Array:
            values = {
                "website_text_status": pa.array(["success", "pending"]),
                "contact_website_text_status": pa.array(["absent", "absent"]),
            }
            return values[name]

        def to_pylist(self) -> list[dict[str, object]]:
            raise AssertionError("resume status checks must not materialize row dictionaries")

    class FakeParquet:
        schema_arrow = POLYGON_PUBLIC_SCHEMA

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield FakeBatch()

    monkeypatch.setattr(workflow.pq, "ParquetFile", lambda _path: FakeParquet())

    assert workflow._shard_needs_enrichment(tmp_path / "source.parquet") is True


def _sources(make_pbf, tmp_path: Path) -> Path:
    first = make_pbf(_WEBSITE_OSM, name="a-latest.osm.pbf")
    second = make_pbf(_EMPTY_OSM, name="b-latest.osm.pbf")
    root = tmp_path / "sources"
    root.mkdir()
    (root / "a-latest.osm.pbf").write_bytes((first / "a-latest.osm.pbf").read_bytes())
    nested = root / "nested"
    nested.mkdir()
    (nested / "b-latest.osm.pbf").write_bytes((second / "b-latest.osm.pbf").read_bytes())
    return root


@pytest.fixture(autouse=True)
def _inject_static_text_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    from osm_polygon_website_tag.application import workflow
    from osm_polygon_website_tag.pipeline.enrich import enrich_polygon_shard as real_enrich

    def enrich(shard, **kwargs):
        return real_enrich(
            shard,
            **kwargs,
            fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"website text"),
            extractor=lambda _html, *, url: TextExtraction(
                "success",
                f"text from {url}",
                count_words(f"text from {url}"),
                None,
                "2.1.0",
            ),
        )

    monkeypatch.setattr(workflow, "enrich_polygon_shard", enrich, raising=False)


def test_discover_sources_is_recursive_sorted_and_rejects_duplicate_names(
    make_pbf,
    tmp_path: Path,
) -> None:
    root = _sources(make_pbf, tmp_path)
    assert [path.name for path in discover_sources(root)] == [
        "a-latest.osm.pbf",
        "b-latest.osm.pbf",
    ]
    duplicate = root / "nested"
    (duplicate / "a-latest.osm.pbf").write_bytes(b"not read")
    with pytest.raises(ValueError, match="duplicate source filenames"):
        discover_sources(root)


def test_run_all_dry_run_completes_without_remote_calls(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow._upload_public_shard",
        lambda *_args: pytest.fail("dry-run must not upload"),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.publish_to_hf",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not publish"),
    )

    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
    )

    assert result.complete
    assert result.extracted_count == 2
    assert result.uploaded_count == 0
    assert load_run(result.run_dir).metadata["status"] == STATUS_COMPLETE


def test_run_all_refreshes_legacy_complete_card_without_reprocessing_sources(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed pre-map run is upgraded locally on the next resume."""
    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    map_path = first.run_dir / "assets" / "geographic_polygon_density.png"
    map_path.unlink()
    receipt_path = first.run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("card_contract_version", None)
    receipt_path.write_text(json.dumps(receipt))

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("legacy card refresh must not read PBFs"),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.enrich_polygon_shard",
        lambda *_args, **_kwargs: pytest.fail("legacy card refresh must not fetch websites"),
    )

    resumed = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert resumed.extracted_count == 0
    assert map_path.is_file()
    assert json.loads(receipt_path.read_text())["card_contract_version"] == 1


def test_run_all_does_not_resume_a_finalized_frozen_snapshot(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalized snapshot is immutable and never retries website failures."""
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    state = load_run(first.run_dir)
    upsert_run_metadata(state, {"snapshot_status": "done"})

    monkeypatch.setattr(
        workflow,
        "discover_sources",
        lambda _root: pytest.fail("frozen resume must not rediscover source PBFs"),
    )
    monkeypatch.setattr(
        workflow,
        "enrich_polygon_shard",
        lambda *_args, **_kwargs: pytest.fail("frozen resume must not retry websites"),
    )
    progress: list[str] = []
    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        progress=progress.append,
    )

    assert resumed.complete
    assert resumed.source_count == 2
    assert resumed.extracted_count == 0
    assert resumed.skipped_count == 0
    assert resumed.uploaded_count == 0
    assert resumed.dry_run is True
    assert load_run(first.run_dir).metadata["snapshot_status"] == "done"
    assert progress == ["Frozen snapshot is already complete; skipping enrichment and uploads"]

    monkeypatch.setattr(
        workflow,
        "resolve_hf_token",
        lambda: pytest.fail("frozen resume must not resolve upload credentials"),
    )
    applied_resume = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    assert applied_resume.complete
    assert applied_resume.dry_run is False
    assert applied_resume.extracted_count == 0
    assert applied_resume.uploaded_count == 0


def test_run_all_resumes_after_ctrl_c(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    original = workflow.extract_pbf
    calls = 0

    def interrupt_second(source: Path, run_dir: Path, run_state=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(source, run_dir, run_state=run_state)

    monkeypatch.setattr(workflow, "extract_pbf", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    run_dir = tmp_path / "runs" / "production"
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING

    monkeypatch.setattr(workflow, "extract_pbf", original)
    result = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert result.complete
    assert result.skipped_count == 1
    assert result.extracted_count == 1


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
        "osm_polygon_website_tag.application.workflow._upload_public_shard",
        lambda _run, source, _repo: shard_uploads.append(source.name),
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
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    events: list[str] = []
    original_extract = workflow.extract_pbf
    original_enrich = workflow.enrich_polygon_shard

    def track_extract(source, *args, **kwargs):
        events.append(f"extract:{Path(source).name}")
        return original_extract(source, *args, **kwargs)

    def track_enrich(shard, *args, **kwargs):
        events.append(f"enrich:{Path(shard).name}")
        return original_enrich(shard, *args, **kwargs)

    monkeypatch.setattr(workflow, "extract_pbf", track_extract)
    monkeypatch.setattr(workflow, "enrich_polygon_shard", track_enrich)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(
        workflow,
        "_upload_public_shard",
        lambda _run, source, _repo: events.append(f"upload:{source.name}"),
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
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    extract_settings: list[dict[str, object]] = []
    enrich_settings: list[dict[str, object]] = []
    original_extract = workflow.extract_pbf
    original_enrich = workflow.enrich_polygon_shard

    def track_extract(source, run_dir, **kwargs):  # type: ignore[no-untyped-def]
        extract_settings.append(dict(kwargs))
        return original_extract(source, run_dir, **kwargs)

    def track_enrich(shard, **kwargs):  # type: ignore[no-untyped-def]
        enrich_settings.append(dict(kwargs))
        return original_enrich(shard, **kwargs)

    monkeypatch.setattr(workflow, "extract_pbf", track_extract)
    monkeypatch.setattr(workflow, "enrich_polygon_shard", track_enrich)

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
    from osm_polygon_website_tag.application import workflow

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
    workflow.extract_pbf(sources[0], run_dir, run_state=state)

    extracted_on_resume: list[str] = []
    original_extract = workflow.extract_pbf

    def track_extract(source, *args, **kwargs):
        extracted_on_resume.append(Path(source).name)
        return original_extract(source, *args, **kwargs)

    monkeypatch.setattr(workflow, "extract_pbf", track_extract)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(workflow, "_upload_public_shard", lambda *_args: None)
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
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    original_enrich = workflow.enrich_polygon_shard
    interrupted = False

    def interrupt_first_enrichment(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_enrich(*args, **kwargs)

    monkeypatch.setattr(workflow, "enrich_polygon_shard", interrupt_first_enrichment)
    with pytest.raises(KeyboardInterrupt):
        run_all(
            source_root=root,
            output_root=tmp_path / "runs",
            run_id="production",
        )

    run_dir = tmp_path / "runs" / "production"
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING
    assert (run_dir / "polygons" / "a-latest.parquet").is_file()

    extracted_on_resume: list[str] = []
    original_extract = workflow.extract_pbf

    def track_extract(source, *args, **kwargs):
        extracted_on_resume.append(Path(source).name)
        return original_extract(source, *args, **kwargs)

    monkeypatch.setattr(workflow, "extract_pbf", track_extract)
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
    source.touch()

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
        "osm_polygon_website_tag.application.workflow.extract_pbf",
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
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    uploads: list[str] = []
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(
        workflow,
        "_upload_public_shard",
        lambda _run, source, _repo: uploads.append(source.name),
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
        workflow,
        "extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("v1.2 migration must not read PBF"),
    )
    monkeypatch.setattr(
        workflow,
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
    captured: list[Path] = []

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    _upload_public_shard(run_dir, Path("source.osm.pbf"), "owner/dataset")

    assert captured == [shard, run_dir / "README.md", run_dir / "dataset.yaml"]


def test_incremental_upload_includes_recomputed_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    shard = run_dir / "polygons" / "source.parquet"
    shard.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), shard)
    (run_dir / "README.md").write_text("card")
    (run_dir / "dataset.yaml").write_text("metadata")
    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir()
    map_path.write_bytes(b"map")
    captured: list[Path] = []

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    _upload_public_shard(run_dir, Path("source.osm.pbf"), "owner/dataset")

    assert captured == [shard, run_dir / "README.md", run_dir / "dataset.yaml", map_path]


def test_resume_enriches_only_shards_with_retryable_text(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    retry_shard = first.run_dir / "polygons" / "a-latest.parquet"
    rows = pq.read_table(retry_shard).to_pylist()
    rows[0]["contact_website_text"] = None
    rows[0]["contact_website_word_count"] = None
    rows[0]["contact_website_text_status"] = "fetch_error"
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), retry_shard)
    state = load_run(first.run_dir)
    update_public_shard_metadata(
        state,
        filename="a-latest.osm.pbf",
        row_count=len(rows),
        shard_sha256=hash_shard(retry_shard),
    )
    original = workflow.enrich_polygon_shard
    enriched: list[str] = []
    bundle_checks: list[str] = []

    original_bundle_check = workflow.source_bundle_is_complete

    def track_bundle_check(run_dir, manifest, fingerprint):
        bundle_checks.append(fingerprint.filename)
        return original_bundle_check(run_dir, manifest, fingerprint)

    monkeypatch.setattr(workflow, "source_bundle_is_complete", track_bundle_check)

    def track(shard, **kwargs):
        enriched.append(Path(shard).name)
        return original(shard, **kwargs)

    monkeypatch.setattr(workflow, "enrich_polygon_shard", track)

    run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert enriched == ["a-latest.parquet"]
    assert bundle_checks == ["a-latest.osm.pbf", "b-latest.osm.pbf"]


def test_run_all_apply_resume_after_keyboard_interrupt_preserves_checkpoint(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume an interleaved extraction after a mid-upload KeyboardInterrupt.

    This characterization test directly exercises the per-shard upload
    checkpoint branch by interrupting the second incremental upload
    and then resuming with ``apply=True``. It protects:

    * checkpoint persistence only after a successful upload,
    * resumption from ``STATUS_EXTRACTING`` while the inventory is incomplete,
    * skipping the already-acknowledged first shard on resume,
    * retrying the interrupted second shard on resume,
    * ``uploaded_count`` counting only the upload performed during
      that invocation,
    * final publication occurring only after successful completion,
    * ``KeyboardInterrupt`` propagation (not swallowed).
    """
    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.resolve_hf_token", lambda: "available"
    )

    shard_uploads: list[str] = []
    final_uploads: list[Path] = []

    interrupted = {"done": False}

    def upload_shard(_run_dir, source, _repo_id):
        shard_uploads.append(source.name)
        # Raise KeyboardInterrupt only once: on the first attempt to upload
        # source "b". The resume invocation should complete normally.
        if source.name == "b-latest.osm.pbf" and not interrupted["done"]:
            interrupted["done"] = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow._upload_public_shard", upload_shard
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.publish_to_hf",
        lambda run_dir, **_kwargs: final_uploads.append(Path(run_dir)),
    )

    with pytest.raises(KeyboardInterrupt):
        run_all(
            source_root=root,
            output_root=tmp_path / "runs",
            run_id="production",
            apply=True,
        )

    run_dir = tmp_path / "runs" / "production"

    # The inventory-level extraction state remains active until every
    # per-source transaction has succeeded.
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING

    # Checkpoint persisted only for the first, successful upload.
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    assert checkpoint_path.is_file()
    checkpoint = json.loads(checkpoint_path.read_text())
    assert set(checkpoint) == {"schema_version", "global_bundle", "sources"}
    assert set(checkpoint["sources"]) == {"a-latest.osm.pbf"}

    # No final publication during the interrupted invocation.
    assert final_uploads == []
    # Both shards reached the upload attempt; the second one raised.
    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    pre_resume_checkpoint = checkpoint_path.read_text()

    # Resume the same run.
    from osm_polygon_website_tag.application import workflow

    resumed_source_calls: list[str] = []
    original_publish = workflow._maybe_publish_enriched_shard

    def track_resume_publish(**kwargs):
        resumed_source_calls.append(Path(kwargs["source"]).name)
        return original_publish(**kwargs)

    monkeypatch.setattr(workflow, "_maybe_publish_enriched_shard", track_resume_publish)
    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    # Only the second shard is uploaded during this invocation.
    assert resumed_source_calls == ["b-latest.osm.pbf"]
    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf", "b-latest.osm.pbf"]
    assert resumed.uploaded_count == 1

    # The checkpoint now covers both sources, and the entry for the
    # already-acknowledged first source is unchanged (byte-identical
    # checkpoint file except for the addition of the second entry).
    final_checkpoint = json.loads(checkpoint_path.read_text())
    assert set(final_checkpoint["sources"]) == {"a-latest.osm.pbf", "b-latest.osm.pbf"}
    # The first source's entry survived intact.
    parsed_pre_resume = json.loads(pre_resume_checkpoint)
    assert (
        final_checkpoint["sources"]["a-latest.osm.pbf"]
        == parsed_pre_resume["sources"]["a-latest.osm.pbf"]
    )

    # Final publication happened exactly once, only after successful
    # completion.
    assert final_uploads == [run_dir]
    assert load_run(run_dir).metadata["status"] == STATUS_COMPLETE


def test_private_workflow_decisions_and_checkpoint_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the small orchestration decision boundaries directly.

    The public workflow tests cover these paths end to end; direct calls keep
    mutation testing honest for the intentionally small private helpers.
    """
    from osm_polygon_website_tag.application import workflow

    state: Any = type("State", (), {})()
    sources: Any = {
        "a.osm.pbf": {"enrichment_pending": False},
        "b.osm.pbf": {"enrichment_pending": True},
    }
    state.sources = sources
    acknowledged = {"a.osm.pbf", "b.osm.pbf", "missing.osm.pbf"}
    processed = workflow._acknowledged_processed_names(state, acknowledged)
    assert processed == {"a.osm.pbf"}
    assert workflow._acknowledged_retry_names(state, acknowledged, processed) == {"b.osm.pbf"}

    source = Path("a.osm.pbf")
    checkpoint: Any = {"schema_version": "v2", "global_bundle": {}, "sources": {}}
    context: Any = type("Context", (), {})()
    context.apply = False
    context.upload_checkpoint = checkpoint
    context.state = state
    context.progress = None
    context.run_dir = tmp_path
    context.repo_id = "owner/dataset"
    context.invocation_id = "run"
    context.area_workers = None
    context.max_in_flight_areas = None
    context.fetch_workers = None
    assert workflow._published_source_names(context, source) is None
    context.apply = True
    assert workflow._published_source_names(context, source) == {"a.osm.pbf"}
    assert workflow._source_requires_publication(
        context=context, migration_changed=False, needs_enrichment=False
    )
    assert workflow._source_requires_publication(
        context=context, migration_changed=True, needs_enrichment=False
    )
    assert (
        workflow._source_requires_publication(
            context=cast(Any, type("Context", (), {"apply": False})()),
            migration_changed=False,
            needs_enrichment=False,
        )
        is False
    )

    manifest_entry: Any = {"public_shard_sha256": "a" * 64}
    sources["a.osm.pbf"] = manifest_entry
    checkpoint["sources"] = {"a.osm.pbf": {"polygon_sha256": "a" * 64}}
    assert workflow._source_upload_is_current(manifest_entry, "a.osm.pbf", checkpoint)
    assert not workflow._source_upload_is_current(manifest_entry, "b.osm.pbf", checkpoint)
    progress: list[str] = []
    context.progress = progress.append
    assert workflow._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=2,
        migration_changed=False,
        needs_enrichment=False,
    )
    assert progress
    workflow._record_source_upload(source, context, uploaded=True)
    assert checkpoint["sources"]["a.osm.pbf"]["polygon_sha256"] == "a" * 64

    assert workflow._should_recheck_enrichment(
        marker=None, status_summary=None, migration_changed=False
    )
    assert not workflow._should_recheck_enrichment(
        marker=False, status_summary={"success": {"count": 1}}, migration_changed=False
    )
    monkeypatch.setattr(workflow, "_shard_needs_enrichment", lambda _path: True)
    decision = workflow._initial_enrichment_decision(
        tmp_path / "a.parquet",
        marker=None,
        status_summary=None,
        migration_changed=False,
    )
    assert decision.needs_enrichment


def test_private_workflow_source_phase_helpers_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.state = type("State", (), {"sources": {"a.osm.pbf": {}}})()
    context.progress = None
    context.repo_id = "owner/dataset"
    context.apply = False
    context.invocation_id = "run"
    context.area_workers = 2
    context.max_in_flight_areas = 3
    context.fetch_workers = 4
    fingerprint: Any = type("Fingerprint", (), {})()
    source = Path("a.osm.pbf")

    extracted: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        workflow,
        "extract_pbf",
        lambda path, run, **kwargs: extracted.append((path, kwargs)),
    )
    workflow._extract_with_options(source, context)
    assert extracted[0][0] == source
    assert extracted[0][1]["area_workers"] == 2

    enrichment_calls: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        workflow,
        "enrich_polygon_shard",
        lambda path, **kwargs: enrichment_calls.append((path, kwargs)) or "enriched",
    )
    assert workflow._enrich_shard(tmp_path / "a.parquet", context) == "enriched"
    assert enrichment_calls[0][1]["fetch_workers"] == 4

    monkeypatch.setattr(workflow, "source_bundle_is_complete", lambda *_args: True)
    bundle = workflow._ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=1,
        total=1,
        allow_extraction=True,
    )
    assert bundle.reused and not bundle.extracted

    monkeypatch.setattr(workflow, "source_bundle_is_complete", lambda *_args: False)
    monkeypatch.setattr(workflow, "_extract_with_options", lambda *_args: None)
    calls = iter([False, True])
    monkeypatch.setattr(workflow, "source_bundle_is_complete", lambda *_args: next(calls))
    bundle = workflow._ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=1,
        total=1,
        allow_extraction=True,
    )
    assert bundle.extracted and not bundle.reused

    original_process_source = workflow._process_source
    monkeypatch.setattr(
        workflow,
        "_process_source",
        lambda **_kwargs: workflow._SourceTransactionResult(True, False, True),
    )
    counts = workflow._process_source_batch(
        [source],
        [source],
        {source.name: fingerprint},
        context,
        allow_extraction=True,
    )
    assert counts == workflow._PhaseCounts(extracted=1, uploaded=1)
    assert workflow._add_phase_counts(
        counts, workflow._PhaseCounts(reused=2)
    ) == workflow._PhaseCounts(extracted=1, reused=2, uploaded=1)

    monkeypatch.setattr(workflow, "_process_source", original_process_source)
    monkeypatch.setattr(workflow.pq, "read_schema", lambda _path: object())
    monkeypatch.setattr(workflow, "schema_matches", lambda *_args: False)
    assert not workflow._migrate_public_shard_if_needed(
        source, tmp_path / "a.parquet", context, 1, 1
    )
    monkeypatch.setattr(
        workflow,
        "_initial_enrichment_decision",
        lambda *_args, **_kwargs: workflow._EnrichmentDecision(False, {"success": {"count": 1}}),
    )
    monkeypatch.setattr(workflow, "update_source_enrichment_status", lambda *_args, **_kwargs: None)
    context.state.sources[source.name] = {"enrichment_pending": False}
    decision = workflow._enrich_source_shard_if_needed(
        source=source,
        shard=tmp_path / "a.parquet",
        context=context,
        index=1,
        total=1,
        migration_changed=False,
    )
    assert not decision.needs_enrichment
    monkeypatch.setattr(
        workflow,
        "_ensure_source_bundle",
        lambda **_kwargs: workflow._SourceBundleResult(tmp_path / "a.parquet", False, True),
    )
    monkeypatch.setattr(workflow, "_migrate_public_shard_if_needed", lambda **_kwargs: False)
    monkeypatch.setattr(
        workflow,
        "_enrich_source_shard_if_needed",
        lambda **_kwargs: workflow._EnrichmentDecision(False, None),
    )
    monkeypatch.setattr(workflow, "_publish_source_if_needed", lambda **_kwargs: True)
    transaction = workflow._process_source(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=1,
        total=1,
        allow_extraction=True,
    )
    assert transaction == workflow._SourceTransactionResult(False, True, True)


def test_private_workflow_publication_and_card_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.state = type(
        "State", (), {"sources": {"a.osm.pbf": {"public_shard_sha256": "a" * 64}}}
    )()
    context.repo_id = "owner/dataset"
    context.apply = False
    context.progress = None
    context.upload_checkpoint = {"schema_version": "v2", "global_bundle": {}, "sources": {}}
    source = Path("a.osm.pbf")

    monkeypatch.setattr(workflow, "_source_upload_is_current_for_context", lambda **_kwargs: True)
    assert not workflow._publish_source_if_needed(
        source=source,
        context=context,
        index=1,
        total=1,
        reused=False,
        migration_changed=False,
        needs_enrichment=False,
    )
    monkeypatch.setattr(workflow, "_source_upload_is_current_for_context", lambda **_kwargs: False)
    monkeypatch.setattr(workflow, "_source_requires_publication", lambda **_kwargs: False)
    assert not workflow._publish_source_if_needed(
        source=source,
        context=context,
        index=1,
        total=1,
        reused=False,
        migration_changed=False,
        needs_enrichment=False,
    )

    monkeypatch.setattr(workflow, "build_card", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        workflow,
        "incremental_publish_changed_shard",
        lambda *_args, **_kwargs: type(
            "Plan", (), {"shard_changed": True, "upload_paths": [source]}
        )(),
    )
    assert not workflow._maybe_publish_enriched_shard(
        run_dir=tmp_path,
        source=source,
        repo_id="owner/dataset",
        apply=False,
        progress=None,
        index=1,
        total=1,
    )
    assert workflow._run_needs_enrichment(tmp_path) is False
    assert workflow._card_refresh_needed(tmp_path) is True


def test_workflow_resume_after_acknowledged_shard_is_skipped(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shard already present in ``uploaded_polygons.json`` with the current
    public shard SHA-256 is skipped on resume: no upload call is made and the
    checkpoint entry is left byte-identical."""
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(workflow, "_upload_public_shard", lambda *_args: None)
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    first = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    checkpoint_path = first.run_dir / "manifests" / "uploaded_polygons.json"
    pre_resume = checkpoint_path.read_text()
    uploaded_during_resume: list[str] = []

    monkeypatch.setattr(
        workflow,
        "_upload_public_shard",
        lambda _run, source, _repo: uploaded_during_resume.append(source.name),
    )

    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    assert uploaded_during_resume == []
    assert resumed.uploaded_count == 0
    assert checkpoint_path.read_text() == pre_resume


def test_workflow_upload_checkpoint_persistence_is_deterministic(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-uploading the same shard on a fresh apply-mode invocation rewrites
    the per-shard checkpoint entry to a deterministic value: identical key
    set, source ordering, and JSON formatting."""
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(workflow, "_upload_public_shard", lambda *_args: None)
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    first = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    checkpoint_path = first.run_dir / "manifests" / "uploaded_polygons.json"
    first_bytes = checkpoint_path.read_text()

    second = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    second_bytes = checkpoint_path.read_text()

    assert first.run_dir == second.run_dir
    assert first_bytes == second_bytes
    parsed = json.loads(first_bytes)
    assert parsed["schema_version"] == "v2"
    assert set(parsed["sources"]) == {"a-latest.osm.pbf", "b-latest.osm.pbf"}
    assert set(parsed) == {"schema_version", "global_bundle", "sources"}
