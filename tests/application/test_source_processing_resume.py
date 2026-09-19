from pathlib import Path
from typing import Any

import pytest

from osm_polygon_website_tag.application import source_processing
from osm_polygon_website_tag.publishing.incremental import IncrementalPublishPlan
from osm_polygon_website_tag.runtime.run_state import SourceFingerprint


def test_source_processing_enrichment_without_cached_summary_recomputes_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy marker without counts is repaired without fetching again."""
    source = Path("a.osm.pbf")
    state: Any = type("State", (), {"sources": {source.name: {"enrichment_pending": False}}})()
    context: Any = type("Context", (), {})()
    context.state = state
    progress: list[str] = []
    context.progress = progress.append
    summaries: list[Path] = []
    statuses: list[dict[str, object]] = []
    initial_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_initial_enrichment_decision",
        lambda shard, **kwargs: (
            initial_calls.append({"shard": shard, **kwargs})
            or source_processing._EnrichmentDecision(False, None)
        ),
    )
    monkeypatch.setattr(
        source_processing,
        "_enrich_shard",
        lambda *_args, **_kwargs: pytest.fail("cached complete shard must not be enriched"),
    )
    monkeypatch.setattr(
        source_processing,
        "summarize_enrichment_status",
        lambda shard: summaries.append(shard) or {"absent": {"count": 1}},
    )
    monkeypatch.setattr(
        source_processing,
        "update_source_enrichment_status",
        lambda state_value, **kwargs: statuses.append({"state": state_value, **kwargs}),
    )

    result = source_processing._enrich_source_shard_if_needed(
        source=source,
        shard=tmp_path / "a.parquet",
        context=context,
        index=1,
        total=1,
        migration_changed=False,
    )

    assert result == source_processing._EnrichmentDecision(False, {"absent": {"count": 1}})
    assert initial_calls == [
        {
            "shard": tmp_path / "a.parquet",
            "marker": False,
            "status_summary": None,
            "migration_changed": False,
        }
    ]
    assert summaries == [tmp_path / "a.parquet"]
    assert statuses == [
        {
            "state": state,
            "filename": source.name,
            "pending": False,
            "status_counts": {"absent": {"count": 1}},
        }
    ]
    assert progress == ["[1/1] Resuming: a.osm.pbf text is complete"]


def test_source_processing_enrichment_resumes_cached_summary_without_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    summary = {"website": {"success": 2}, "contact_website": {"success": 1}}
    state: Any = type(
        "State",
        (),
        {
            "sources": {
                source.name: {
                    "enrichment_pending": False,
                    "enrichment_status_counts": summary,
                }
            }
        },
    )()
    context: Any = type("Context", (), {})()
    context.state = state
    progress: list[str] = []
    context.progress = progress.append
    status_calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        source_processing,
        "_enrich_shard",
        lambda *_args, **_kwargs: pytest.fail("cached complete shard must not be enriched"),
    )
    monkeypatch.setattr(
        source_processing,
        "summarize_enrichment_status",
        lambda _shard: pytest.fail("cached summary must avoid a status scan"),
    )
    monkeypatch.setattr(
        source_processing,
        "update_source_enrichment_status",
        lambda state_value, **kwargs: status_calls.append((state_value, kwargs)),
    )

    result = source_processing._enrich_source_shard_if_needed(
        source=source,
        shard=tmp_path / "a.parquet",
        context=context,
        index=2,
        total=3,
        migration_changed=False,
    )

    assert result == source_processing._EnrichmentDecision(False, summary)
    assert status_calls == [
        (
            state,
            {
                "filename": source.name,
                "pending": False,
                "status_counts": summary,
            },
        )
    ]
    assert progress == ["[2/3] Resuming: a.osm.pbf text is complete"]


def test_source_processing_publication_forwards_resume_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required publication receives the correct resume and source set."""
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.repo_id = "owner/dataset"
    context.apply = True
    context.progress = None
    context.state = type(
        "State", (), {"sources": {source.name: {"public_shard_sha256": "a" * 64}}}
    )()
    context.upload_checkpoint = {
        "schema_version": "v2",
        "global_bundle": {},
        "sources": {"previous.osm.pbf": {"polygon_sha256": "c" * 64}},
    }
    calls: list[dict[str, object]] = []
    recorded: list[tuple[Path, Any, bool]] = []
    monkeypatch.setattr(
        source_processing, "_source_upload_is_current_for_context", lambda **_kwargs: False
    )
    monkeypatch.setattr(source_processing, "_source_requires_publication", lambda **_kwargs: True)
    monkeypatch.setattr(
        source_processing,
        "_maybe_publish_enriched_shard",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    monkeypatch.setattr(
        source_processing,
        "_record_source_upload",
        lambda source_value, context_value, uploaded: recorded.append(
            (source_value, context_value, uploaded)
        ),
    )

    result = source_processing._publish_source_if_needed(
        source=source,
        context=context,
        index=3,
        total=4,
        reused=True,
        migration_changed=False,
        needs_enrichment=True,
    )

    assert result is True
    assert calls == [
        {
            "run_dir": tmp_path,
            "source": source,
            "repo_id": "owner/dataset",
            "apply": True,
            "progress": None,
            "index": 3,
            "total": 4,
            "allow_bundle_only": False,
            "published_source_names": {"previous.osm.pbf", "a.osm.pbf"},
        }
    ]
    assert recorded == [(source, context, True)]


def test_source_processing_publication_reuses_incremental_plan_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upload and checkpoint use the already-computed incremental plan."""
    source = Path("a.osm.pbf")
    plan = IncrementalPublishPlan(
        source_filename=source.name,
        upload_paths=[tmp_path / "polygons" / "a.parquet"],
        shard_changed=True,
        bundle_changed=True,
        shard_sha256="a" * 64,
        bundle_state={
            "readme_sha256": "b" * 64,
            "dataset_yaml_sha256": "c" * 64,
            "map_sha256": "d" * 64,
            "map_contract_version": 1,
        },
    )
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.repo_id = "owner/dataset"
    context.apply = True
    context.progress = None
    uploads: list[tuple[tuple[object, ...], dict[str, object]]] = []
    persisted: list[dict[str, object]] = []
    monkeypatch.setattr(source_processing, "build_card", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        source_processing, "incremental_publish_changed_shard", lambda *_args, **_kwargs: plan
    )
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda *args, **kwargs: uploads.append((args, kwargs)),
    )
    monkeypatch.setattr(
        source_processing,
        "persist_successful_upload",
        lambda *_args, **kwargs: persisted.append(kwargs),
    )

    assert (
        source_processing._maybe_publish_enriched_shard(
            run_dir=tmp_path,
            source=source,
            repo_id="owner/dataset",
            apply=True,
            progress=None,
            index=1,
            total=1,
        )
        is True
    )

    assert len(uploads) == 1
    assert uploads[0][0][-1] is plan
    assert uploads[0][1] == {}
    assert persisted == [{"shard_sha256": plan.shard_sha256, "bundle_state": plan.bundle_state}]


def test_process_source_coordinates_the_source_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    fingerprint = SourceFingerprint(source.name, 1, 2)
    context: Any = type("Context", (), {})()
    calls: list[tuple[str, dict[str, object]]] = []
    bundle = source_processing._SourceBundleResult(tmp_path / "a.parquet", True, False)
    decision = source_processing._EnrichmentDecision(True, {"retry": {"count": 1}})

    monkeypatch.setattr(
        source_processing,
        "_ensure_source_bundle",
        lambda **kwargs: calls.append(("ensure", kwargs)) or bundle,
    )
    monkeypatch.setattr(
        source_processing,
        "_migrate_public_shard_if_needed",
        lambda **kwargs: calls.append(("migrate", kwargs)) or True,
    )
    monkeypatch.setattr(
        source_processing,
        "_enrich_source_shard_if_needed",
        lambda **kwargs: calls.append(("enrich", kwargs)) or decision,
    )
    monkeypatch.setattr(
        source_processing,
        "_detect_source_shard_if_needed",
        lambda **kwargs: calls.append(("detect", kwargs)) or True,
    )
    monkeypatch.setattr(
        source_processing,
        "_publish_source_if_needed",
        lambda **kwargs: calls.append(("publish", kwargs)) or True,
    )

    result = source_processing._process_source(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=2,
        total=3,
        allow_extraction=False,
    )

    assert result == source_processing._SourceTransactionResult(True, False, True)
    assert [name for name, _kwargs in calls] == ["ensure", "migrate", "enrich", "detect", "publish"]
    assert calls[0][1] == {
        "source": source,
        "fingerprint": fingerprint,
        "context": context,
        "index": 2,
        "total": 3,
        "allow_extraction": False,
    }
    assert calls[1][1] == {
        "source": source,
        "shard": bundle.shard,
        "context": context,
        "index": 2,
        "total": 3,
    }
    assert calls[2][1] == {
        "source": source,
        "shard": bundle.shard,
        "context": context,
        "index": 2,
        "total": 3,
        "migration_changed": True,
    }
    assert calls[3][1] == {
        "source": source,
        "shard": bundle.shard,
        "context": context,
        "index": 2,
        "total": 3,
    }
    assert calls[4][1] == {
        "source": source,
        "context": context,
        "index": 2,
        "total": 3,
        "reused": False,
        "migration_changed": True,
        "needs_enrichment": True,
        "language_changed": True,
    }


def test_ensure_source_bundle_distinguishes_reuse_and_incomplete_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    fingerprint: Any = object()
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.state = type("State", (), {"sources": {}})()
    context.progress = None

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", lambda *_args: True)
    reused = source_processing._ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=1,
        total=1,
        allow_extraction=False,
    )
    assert reused == source_processing._SourceBundleResult(
        tmp_path / "polygons" / "a.parquet", False, False
    )

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", lambda *_args: False)
    with pytest.raises(ValueError, match="cannot enrich incomplete"):
        source_processing._ensure_source_bundle(
            source=source,
            fingerprint=fingerprint,
            context=context,
            index=1,
            total=1,
            allow_extraction=False,
        )


def test_ensure_source_bundle_reports_failed_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.state = type("State", (), {"sources": {}})()
    context.progress = None
    monkeypatch.setattr(source_processing, "source_bundle_is_complete", lambda *_args: False)
    monkeypatch.setattr(source_processing, "_extract_with_options", lambda *_args: None)

    with pytest.raises(ValueError, match="incomplete after extraction"):
        source_processing._ensure_source_bundle(
            source=source,
            fingerprint=SourceFingerprint(source.name, 0, 0),
            context=context,
            index=1,
            total=1,
            allow_extraction=True,
        )


def test_ensure_source_bundle_reuses_complete_bundle_with_exact_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    fingerprint = SourceFingerprint(source.name, 1, 2)
    manifest_entry = {"source_sha256": "a" * 64}
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.state = type("State", (), {"sources": {source.name: manifest_entry}})()
    progress: list[str] = []
    context.progress = progress.append
    completeness_calls: list[tuple[Path, object, SourceFingerprint]] = []

    def complete(
        run_dir: Path,
        entry: object,
        fingerprint_value: SourceFingerprint,
    ) -> bool:
        completeness_calls.append((run_dir, entry, fingerprint_value))
        return True

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", complete)

    result = source_processing._ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=2,
        total=3,
        allow_extraction=True,
    )

    assert result == source_processing._SourceBundleResult(
        tmp_path / "polygons" / "a.parquet", False, True
    )
    assert completeness_calls == [(tmp_path, manifest_entry, fingerprint)]
    assert progress == ["[2/3] Resuming: a.osm.pbf is complete"]


def test_ensure_source_bundle_extracts_and_rechecks_exact_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    fingerprint = SourceFingerprint(source.name, 1, 2)
    manifest_entry = {"source_sha256": "a" * 64}
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.state = type("State", (), {"sources": {source.name: manifest_entry}})()
    progress: list[str] = []
    context.progress = progress.append
    completeness_calls: list[tuple[Path, object, SourceFingerprint]] = []
    complete_results = iter((False, True))

    def complete(
        run_dir: Path,
        entry: object,
        fingerprint_value: SourceFingerprint,
    ) -> bool:
        completeness_calls.append((run_dir, entry, fingerprint_value))
        return next(complete_results)

    extraction_calls: list[tuple[Path, object]] = []

    def extract(source_value: Path, context_value: object) -> None:
        extraction_calls.append((source_value, context_value))

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", complete)
    monkeypatch.setattr(source_processing, "_extract_with_options", extract)

    result = source_processing._ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=2,
        total=3,
        allow_extraction=True,
    )

    assert result == source_processing._SourceBundleResult(
        tmp_path / "polygons" / "a.parquet", True, False
    )
    assert completeness_calls == [
        (tmp_path, manifest_entry, fingerprint),
        (tmp_path, manifest_entry, fingerprint),
    ]
    assert extraction_calls == [(source, context)]
    assert progress == ["[2/3] Extracting a.osm.pbf"]


def test_extract_with_options_omits_unset_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = object()
    context: Any = type("Context", (), {})()
    context.state = state
    context.run_dir = tmp_path
    context.area_workers = None
    context.max_in_flight_areas = None
    calls: list[tuple[Path, Path, dict[str, object]]] = []
    monkeypatch.setattr(
        source_processing,
        "extract_pbf",
        lambda source, run_dir, **kwargs: calls.append((source, run_dir, kwargs)),
    )

    source_processing._extract_with_options(Path("a.osm.pbf"), context)

    assert calls == [(Path("a.osm.pbf"), tmp_path, {"run_state": state})]
