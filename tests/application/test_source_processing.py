from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.application.source_processing import SourceProcessingContext
from osm_polygon_website_tag.publishing.incremental import CheckpointV2, IncrementalPublishPlan
from osm_polygon_website_tag.runtime.run_state import RunState, SourceFingerprint


def test_process_sources_returns_counts_in_order(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "first.osm.pbf"
    second = tmp_path / "second.osm.pbf"
    calls: list[tuple[str, int, int, bool]] = []

    def process_source(**kwargs: object) -> SimpleNamespace:
        source = kwargs["source"]
        index = kwargs["index"]
        total = kwargs["total"]
        allow_extraction = kwargs["allow_extraction"]
        assert isinstance(source, Path)
        assert isinstance(index, int)
        assert isinstance(total, int)
        assert isinstance(allow_extraction, bool)
        calls.append((source.name, index, total, allow_extraction))
        return SimpleNamespace(extracted=index == 1, reused=index == 2, uploaded=True)

    context = SourceProcessingContext(
        run_dir=tmp_path,
        state=RunState(run_dir=tmp_path, run_id="test"),
        repo_id="owner/dataset",
        apply=False,
        progress=None,
        invocation_id="test",
        upload_checkpoint=CheckpointV2(schema_version="v2", global_bundle={}, sources={}),
        area_workers=None,
        max_in_flight_areas=None,
        fetch_workers=None,
        detect_languages=False,
        language_detector=None,
    )
    monkeypatch.setattr(source_processing, "_process_source", process_source, raising=False)
    result = source_processing.process_sources(
        sources=[first, second],
        ordered_sources=[second, first],
        fingerprints_by_name={
            "first.osm.pbf": SourceFingerprint("first.osm.pbf", 0, 0),
            "second.osm.pbf": SourceFingerprint("second.osm.pbf", 0, 0),
        },
        context=context,
        allow_extraction=False,
    )

    assert calls == [
        ("second.osm.pbf", 1, 2, False),
        ("first.osm.pbf", 2, 2, False),
    ]
    assert result.extracted == 1
    assert result.reused == 1
    assert result.uploaded == 2


def test_source_processing_decisions_and_checkpoint_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise source publication and enrichment decision boundaries directly."""
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
    assert source_processing._published_source_names(context, source) is None
    context.apply = True
    assert source_processing._published_source_names(context, source) == {"a.osm.pbf"}
    assert source_processing._source_requires_publication(
        context=context, migration_changed=False, needs_enrichment=False
    )
    assert source_processing._source_requires_publication(
        context=context, migration_changed=True, needs_enrichment=False
    )
    assert (
        source_processing._source_requires_publication(
            context=cast(Any, type("Context", (), {"apply": False})()),
            migration_changed=False,
            needs_enrichment=False,
        )
        is False
    )

    manifest_entry: Any = {"public_shard_sha256": "a" * 64}
    sources["a.osm.pbf"] = manifest_entry
    checkpoint["sources"] = {"a.osm.pbf": {"polygon_sha256": "a" * 64}}
    assert source_processing._source_upload_is_current(manifest_entry, "a.osm.pbf", checkpoint)
    assert not source_processing._source_upload_is_current(manifest_entry, "b.osm.pbf", checkpoint)
    progress: list[str] = []
    context.progress = progress.append
    assert source_processing._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=2,
        migration_changed=False,
        needs_enrichment=False,
    )
    assert progress
    source_processing._record_source_upload(source, context, uploaded=True)
    assert checkpoint["sources"]["a.osm.pbf"]["polygon_sha256"] == "a" * 64

    assert source_processing._should_recheck_enrichment(
        marker=None, status_summary=None, migration_changed=False
    )
    assert not source_processing._should_recheck_enrichment(
        marker=False, status_summary={"success": {"count": 1}}, migration_changed=False
    )
    monkeypatch.setattr(source_processing, "_shard_needs_enrichment", lambda _path: True)
    decision = source_processing._initial_enrichment_decision(
        tmp_path / "a.parquet",
        marker=None,
        status_summary=None,
        migration_changed=False,
    )
    assert decision.needs_enrichment


def test_source_processing_phase_helpers_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        source_processing,
        "extract_pbf",
        lambda path, run, **kwargs: extracted.append((path, kwargs)),
    )
    source_processing._extract_with_options(source, context)
    assert extracted[0][0] == source
    assert extracted[0][1]["area_workers"] == 2

    enrichment_calls: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        source_processing,
        "enrich_polygon_shard",
        lambda path, **kwargs: enrichment_calls.append((path, kwargs)) or "enriched",
    )
    assert source_processing._enrich_shard(tmp_path / "a.parquet", context) == "enriched"
    assert enrichment_calls[0][1]["fetch_workers"] == 4

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", lambda *_args: True)
    bundle = source_processing._ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=1,
        total=1,
        allow_extraction=True,
    )
    assert bundle.reused and not bundle.extracted

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", lambda *_args: False)
    monkeypatch.setattr(source_processing, "_extract_with_options", lambda *_args: None)
    calls = iter([False, True])
    monkeypatch.setattr(source_processing, "source_bundle_is_complete", lambda *_args: next(calls))
    bundle = source_processing._ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=1,
        total=1,
        allow_extraction=True,
    )
    assert bundle.extracted and not bundle.reused

    original_process_source = source_processing._process_source
    monkeypatch.setattr(
        source_processing,
        "_process_source",
        lambda **_kwargs: source_processing._SourceTransactionResult(True, False, True),
    )
    counts = source_processing.process_sources(
        sources=[source],
        ordered_sources=[source],
        fingerprints_by_name={source.name: fingerprint},
        context=context,
        allow_extraction=True,
    )
    assert counts == source_processing.SourcePhaseCounts(extracted=1, uploaded=1)
    assert workflow._add_phase_counts(
        counts, source_processing.SourcePhaseCounts(reused=2)
    ) == source_processing.SourcePhaseCounts(extracted=1, reused=2, uploaded=1)

    monkeypatch.setattr(source_processing, "_process_source", original_process_source)
    monkeypatch.setattr(source_processing.pq, "read_schema", lambda _path: object())
    monkeypatch.setattr(source_processing, "schema_matches", lambda *_args: False)
    assert not source_processing._migrate_public_shard_if_needed(
        source, tmp_path / "a.parquet", context, 1, 1
    )
    monkeypatch.setattr(
        source_processing,
        "_initial_enrichment_decision",
        lambda *_args, **_kwargs: source_processing._EnrichmentDecision(
            False, {"success": {"count": 1}}
        ),
    )
    monkeypatch.setattr(
        source_processing, "update_source_enrichment_status", lambda *_args, **_kwargs: None
    )
    context.state.sources[source.name] = {"enrichment_pending": False}
    decision = source_processing._enrich_source_shard_if_needed(
        source=source,
        shard=tmp_path / "a.parquet",
        context=context,
        index=1,
        total=1,
        migration_changed=False,
    )
    assert not decision.needs_enrichment
    monkeypatch.setattr(
        source_processing,
        "_ensure_source_bundle",
        lambda **_kwargs: source_processing._SourceBundleResult(
            tmp_path / "a.parquet", False, True
        ),
    )
    monkeypatch.setattr(
        source_processing, "_migrate_public_shard_if_needed", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        source_processing,
        "_enrich_source_shard_if_needed",
        lambda **_kwargs: source_processing._EnrichmentDecision(False, None),
    )
    monkeypatch.setattr(source_processing, "_publish_source_if_needed", lambda **_kwargs: True)
    transaction = source_processing._process_source(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=1,
        total=1,
        allow_extraction=True,
    )
    assert transaction == source_processing._SourceTransactionResult(False, True, True)


def test_source_processing_publication_and_card_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        source_processing, "_source_upload_is_current_for_context", lambda **_kwargs: True
    )
    assert not source_processing._publish_source_if_needed(
        source=source,
        context=context,
        index=1,
        total=1,
        reused=False,
        migration_changed=False,
        needs_enrichment=False,
    )
    monkeypatch.setattr(
        source_processing, "_source_upload_is_current_for_context", lambda **_kwargs: False
    )
    monkeypatch.setattr(source_processing, "_source_requires_publication", lambda **_kwargs: False)
    assert not source_processing._publish_source_if_needed(
        source=source,
        context=context,
        index=1,
        total=1,
        reused=False,
        migration_changed=False,
        needs_enrichment=False,
    )

    monkeypatch.setattr(source_processing, "build_card", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        source_processing,
        "incremental_publish_changed_shard",
        lambda *_args, **_kwargs: type(
            "Plan", (), {"shard_changed": True, "upload_paths": [source]}
        )(),
    )
    assert not source_processing._maybe_publish_enriched_shard(
        run_dir=tmp_path,
        source=source,
        repo_id="owner/dataset",
        apply=False,
        progress=None,
        index=1,
        total=1,
    )
    assert source_processing._run_needs_enrichment(tmp_path) is False
    assert workflow._card_refresh_needed(tmp_path) is True


def test_source_processing_enrichment_branch_persists_result_and_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enrichment branch records both shard metadata and retry status."""
    source = Path("a.osm.pbf")
    state: Any = type("State", (), {"sources": {source.name: {"enrichment_pending": True}}})()
    context: Any = type("Context", (), {})()
    context.state = state
    context.run_dir = tmp_path
    progress: list[str] = []
    context.progress = progress.append
    context.invocation_id = "invocation"
    context.fetch_workers = None

    enrichment_calls: list[tuple[Path, Any]] = []
    metadata_calls: list[dict[str, object]] = []
    status_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_initial_enrichment_decision",
        lambda *_args, **_kwargs: source_processing._EnrichmentDecision(True, None),
    )
    monkeypatch.setattr(
        source_processing,
        "_enrich_shard",
        lambda shard, context: (
            enrichment_calls.append((shard, context))
            or type("Enrichment", (), {"row_count": 7, "shard_sha256": "b" * 64})()
        ),
    )
    monkeypatch.setattr(source_processing, "_shard_needs_enrichment", lambda _shard: False)
    monkeypatch.setattr(
        source_processing,
        "summarize_enrichment_status",
        lambda _shard: {"success": {"count": 7}},
    )
    monkeypatch.setattr(
        source_processing,
        "update_public_shard_metadata",
        lambda _state, **kwargs: metadata_calls.append(kwargs),
    )
    monkeypatch.setattr(
        source_processing,
        "update_source_enrichment_status",
        lambda _state, **kwargs: status_calls.append(kwargs),
    )

    result = source_processing._enrich_source_shard_if_needed(
        source=source,
        shard=tmp_path / "a.parquet",
        context=context,
        index=2,
        total=3,
        migration_changed=False,
    )

    assert result == source_processing._EnrichmentDecision(False, {"success": {"count": 7}})
    assert enrichment_calls == [(tmp_path / "a.parquet", context)]
    assert metadata_calls == [{"filename": source.name, "row_count": 7, "shard_sha256": "b" * 64}]
    assert status_calls == [
        {
            "filename": source.name,
            "pending": False,
            "status_counts": {"success": {"count": 7}},
        }
    ]
    assert progress == ["[2/3] Enriching a.osm.pbf"]


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
    monkeypatch.setattr(
        source_processing,
        "_initial_enrichment_decision",
        lambda *_args, **_kwargs: source_processing._EnrichmentDecision(False, None),
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
        lambda _state, **kwargs: statuses.append(kwargs),
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
    assert summaries == [tmp_path / "a.parquet"]
    assert statuses == [
        {
            "filename": source.name,
            "pending": False,
            "status_counts": {"absent": {"count": 1}},
        }
    ]
    assert progress == ["[1/1] Resuming: a.osm.pbf text is complete"]


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
