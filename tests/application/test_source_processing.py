from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.application.source_processing import SourceProcessingContext
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.publishing.incremental import CheckpointV2, IncrementalPublishPlan
from osm_polygon_website_tag.runtime.run_state import RunState, SourceFingerprint


def test_process_sources_returns_counts_in_order(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "first.osm.pbf"
    second = tmp_path / "second.osm.pbf"
    calls: list[dict[str, object]] = []

    def process_source(**kwargs: object) -> SimpleNamespace:
        source = kwargs["source"]
        index = kwargs["index"]
        total = kwargs["total"]
        allow_extraction = kwargs["allow_extraction"]
        assert isinstance(source, Path)
        assert isinstance(index, int)
        assert isinstance(total, int)
        assert isinstance(allow_extraction, bool)
        calls.append(kwargs)
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
        {
            "source": second,
            "fingerprint": SourceFingerprint("second.osm.pbf", 0, 0),
            "context": context,
            "index": 1,
            "total": 2,
            "allow_extraction": False,
        },
        {
            "source": first,
            "fingerprint": SourceFingerprint("first.osm.pbf", 0, 0),
            "context": context,
            "index": 2,
            "total": 2,
            "allow_extraction": False,
        },
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
    assert progress == ["[1/2] Resuming: a.osm.pbf is already uploaded"]
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
    context.detect_languages = False
    context.language_detector = None
    fingerprint: Any = type("Fingerprint", (), {})()
    source = Path("a.osm.pbf")

    extracted: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        source_processing,
        "extract_pbf",
        lambda path, run, **kwargs: extracted.append((path, kwargs)),
    )
    source_processing._extract_with_options(source, context)
    assert extracted == [
        (
            source,
            {
                "run_state": context.state,
                "area_workers": 2,
                "max_in_flight_areas": 3,
            },
        )
    ]

    enrichment_calls: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        source_processing,
        "enrich_polygon_shard",
        lambda path, **kwargs: enrichment_calls.append((path, kwargs)) or "enriched",
    )
    assert source_processing._enrich_shard(tmp_path / "a.parquet", context) == "enriched"
    assert enrichment_calls == [
        (
            tmp_path / "a.parquet",
            {
                "cache_path": tmp_path / "cache" / "website_text.sqlite3",
                "invocation_id": "run",
                "fetch_workers": 4,
            },
        )
    ]

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
    state: Any = type(
        "State",
        (),
        {
            "sources": {
                source.name: {
                    "enrichment_pending": True,
                    "enrichment_status_counts": {
                        "website": {"retry": 1},
                        "contact_website": {"retry": 1},
                    },
                }
            }
        },
    )()
    context: Any = type("Context", (), {})()
    context.state = state
    context.run_dir = tmp_path
    progress: list[str] = []
    context.progress = progress.append
    context.invocation_id = "invocation"
    context.fetch_workers = None

    enrichment_calls: list[tuple[Path, Any]] = []
    initial_calls: list[dict[str, object]] = []
    metadata_calls: list[tuple[object, dict[str, object]]] = []
    status_calls: list[tuple[object, dict[str, object]]] = []

    def initial_decision(
        shard_value: Path,
        *,
        marker: object,
        status_summary: dict[str, dict[str, int]] | None,
        migration_changed: bool,
    ) -> source_processing._EnrichmentDecision:
        initial_calls.append(
            {
                "shard": shard_value,
                "marker": marker,
                "status_summary": status_summary,
                "migration_changed": migration_changed,
            }
        )
        return source_processing._EnrichmentDecision(True, status_summary)

    monkeypatch.setattr(source_processing, "_initial_enrichment_decision", initial_decision)
    monkeypatch.setattr(
        source_processing,
        "_enrich_shard",
        lambda shard, context: (
            enrichment_calls.append((shard, context))
            or type("Enrichment", (), {"row_count": 7, "shard_sha256": "b" * 64})()
        ),
    )
    shard_checks: list[Path] = []

    def shard_needs_enrichment(shard_value: Path) -> bool:
        shard_checks.append(shard_value)
        return False

    monkeypatch.setattr(source_processing, "_shard_needs_enrichment", shard_needs_enrichment)
    summary_calls: list[Path] = []

    def summarize(shard_value: Path) -> dict[str, dict[str, int]]:
        summary_calls.append(shard_value)
        return {"website": {"success": 7}, "contact_website": {"success": 7}}

    monkeypatch.setattr(
        source_processing,
        "summarize_enrichment_status",
        summarize,
    )
    monkeypatch.setattr(
        source_processing,
        "update_public_shard_metadata",
        lambda state_value, **kwargs: metadata_calls.append((state_value, kwargs)),
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

    expected_summary = {"website": {"success": 7}, "contact_website": {"success": 7}}
    assert result == source_processing._EnrichmentDecision(False, expected_summary)
    assert initial_calls == [
        {
            "shard": tmp_path / "a.parquet",
            "marker": True,
            "status_summary": {
                "website": {"retry": 1},
                "contact_website": {"retry": 1},
            },
            "migration_changed": False,
        }
    ]
    assert enrichment_calls == [(tmp_path / "a.parquet", context)]
    assert shard_checks == [tmp_path / "a.parquet"]
    assert summary_calls == [tmp_path / "a.parquet"]
    assert metadata_calls == [
        (
            state,
            {"filename": source.name, "row_count": 7, "shard_sha256": "b" * 64},
        )
    ]
    assert status_calls == [
        (
            state,
            {
                "filename": source.name,
                "pending": False,
                "status_counts": expected_summary,
            },
        )
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


def test_migrate_public_shard_updates_manifest_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    shard = tmp_path / "a.parquet"
    context: Any = type("Context", (), {})()
    context.state = object()
    progress: list[str] = []
    context.progress = progress.append
    schema = object()
    read_calls: list[Path] = []
    schema_calls: list[tuple[object, object]] = []
    migration_calls: list[Path] = []
    metadata: list[tuple[object, dict[str, object]]] = []

    def read_schema(path: Path) -> object:
        read_calls.append(path)
        return schema

    def matches(actual: object, expected: object) -> bool:
        schema_calls.append((actual, expected))
        return expected is POLYGON_PUBLIC_SCHEMA_V1_2

    monkeypatch.setattr(source_processing.pq, "read_schema", read_schema)
    monkeypatch.setattr(source_processing, "schema_matches", matches)
    monkeypatch.setattr(
        source_processing,
        "migrate_public_shard",
        lambda path: (
            migration_calls.append(path)
            or SimpleNamespace(changed=True, row_count=4, shard_sha256="a" * 64)
        ),
    )
    monkeypatch.setattr(
        source_processing,
        "update_public_shard_metadata",
        lambda state, **kwargs: metadata.append((state, kwargs)),
    )

    assert source_processing._migrate_public_shard_if_needed(source, shard, context, 2, 3)
    assert read_calls == [shard]
    assert schema_calls == [(schema, POLYGON_PUBLIC_SCHEMA_V1_2)]
    assert migration_calls == [shard]
    assert progress == ["[2/3] Migrating a.osm.pbf to public schema v1.3"]
    assert metadata == [
        (
            context.state,
            {"filename": source.name, "row_count": 4, "shard_sha256": "a" * 64},
        )
    ]


def test_detect_source_shard_handles_opt_in_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    shard = tmp_path / "a.parquet"
    context: Any = type("Context", (), {})()
    context.progress = None
    context.state = object()
    context.language_detector = None
    context.detect_languages = False
    monkeypatch.setattr(
        source_processing,
        "shard_needs_language_detection",
        lambda _path: pytest.fail("disabled detection must not inspect the shard"),
    )
    assert not source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )

    context.detect_languages = True
    inspected: list[Path] = []

    def needs_detection(path: Path) -> bool:
        inspected.append(path)
        return False

    monkeypatch.setattr(source_processing, "shard_needs_language_detection", needs_detection)
    assert not source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )
    assert inspected == [shard]

    monkeypatch.setattr(source_processing, "shard_needs_language_detection", lambda _path: True)
    with pytest.raises(ValueError) as error:
        source_processing._detect_source_shard_if_needed(
            source=source, shard=shard, context=context, index=1, total=1
        )
    assert str(error.value) == "language detection requested without a detector"


def test_detect_source_shard_persists_changed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    shard = tmp_path / "a.parquet"
    context: Any = type("Context", (), {})()
    progress: list[str] = []
    context.progress = progress.append
    context.state = object()
    context.detect_languages = True
    context.language_detector = object()
    metadata: list[tuple[object, dict[str, object]]] = []
    detector_calls: list[tuple[Path, object]] = []

    inspected: list[Path] = []

    def needs_detection(path: Path) -> bool:
        inspected.append(path)
        return True

    monkeypatch.setattr(source_processing, "shard_needs_language_detection", needs_detection)
    monkeypatch.setattr(
        source_processing,
        "detect_language_shard",
        lambda path, *, detector: (
            detector_calls.append((path, detector))
            or SimpleNamespace(changed=True, row_count=4, shard_sha256="b" * 64)
        ),
    )
    monkeypatch.setattr(
        source_processing,
        "update_public_shard_metadata",
        lambda state_value, **kwargs: metadata.append((state_value, kwargs)),
    )

    assert source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )
    assert inspected == [shard]
    assert detector_calls == [(shard, context.language_detector)]
    assert metadata == [
        (
            context.state,
            {"filename": source.name, "row_count": 4, "shard_sha256": "b" * 64},
        )
    ]
    assert progress == ["[1/1] Detecting languages for a.osm.pbf"]


@pytest.mark.parametrize(
    ("marker", "status_summary", "migration_changed", "expected"),
    [
        (None, None, False, True),
        (True, {"success": {"count": 1}}, False, False),
        (False, {"success": {"count": 1}}, False, False),
        (False, {"success": {"count": 1}}, True, True),
        (False, None, False, True),
        (True, None, False, False),
    ],
)
def test_should_recheck_enrichment_is_explicit(
    marker: object,
    status_summary: dict[str, dict[str, int]] | None,
    migration_changed: bool,
    expected: bool,
) -> None:
    assert (
        source_processing._should_recheck_enrichment(
            marker=marker,
            status_summary=status_summary,
            migration_changed=migration_changed,
        )
        is expected
    )


def test_initial_enrichment_decision_preserves_the_cached_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = {"success": {"count": 2}}
    monkeypatch.setattr(
        source_processing,
        "_shard_needs_enrichment",
        lambda _path: pytest.fail("cached marker should avoid a shard scan"),
    )

    decision = source_processing._initial_enrichment_decision(
        tmp_path / "a.parquet",
        marker=False,
        status_summary=summary,
        migration_changed=False,
    )

    assert decision == source_processing._EnrichmentDecision(False, summary)


def test_initial_enrichment_decision_rechecks_after_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "a.parquet"
    monkeypatch.setattr(
        source_processing,
        "_shard_needs_enrichment",
        lambda path: (path == shard) or pytest.fail(f"unexpected shard: {path}"),
    )

    decision = source_processing._initial_enrichment_decision(
        shard,
        marker=False,
        status_summary={"success": {"count": 2}},
        migration_changed=True,
    )

    assert decision == source_processing._EnrichmentDecision(True, {"success": {"count": 2}})


def test_publish_source_forwards_all_change_reasons_and_resume_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.repo_id = "owner/dataset"
    context.apply = False
    callback_messages: list[str] = []
    context.progress = callback_messages.append
    context.state = type("State", (), {"sources": {source.name: {}}})()
    context.upload_checkpoint = {"sources": {}}
    current_calls: list[dict[str, object]] = []
    publication_calls: list[dict[str, object]] = []
    publish_calls: list[dict[str, object]] = []
    record_calls: list[tuple[Path, Any, bool]] = []
    monkeypatch.setattr(
        source_processing,
        "_source_upload_is_current_for_context",
        lambda **kwargs: current_calls.append(kwargs) or False,
    )
    monkeypatch.setattr(
        source_processing,
        "_source_requires_publication",
        lambda **kwargs: publication_calls.append(kwargs) or True,
    )
    monkeypatch.setattr(
        source_processing,
        "_maybe_publish_enriched_shard",
        lambda **kwargs: publish_calls.append(kwargs) or False,
    )
    monkeypatch.setattr(
        source_processing,
        "_record_source_upload",
        lambda source_value, context_value, uploaded: record_calls.append(
            (source_value, context_value, uploaded)
        ),
    )

    result = source_processing._publish_source_if_needed(
        source=source,
        context=context,
        index=2,
        total=3,
        reused=False,
        migration_changed=True,
        needs_enrichment=False,
        language_changed=True,
    )

    assert result is False
    assert current_calls == [
        {
            "source": source,
            "context": context,
            "index": 2,
            "total": 3,
            "migration_changed": True,
            "needs_enrichment": False,
            "language_changed": True,
        }
    ]
    assert publication_calls == [
        {
            "context": context,
            "migration_changed": True,
            "needs_enrichment": False,
            "language_changed": True,
        }
    ]
    assert publish_calls == [
        {
            "run_dir": tmp_path,
            "source": source,
            "repo_id": "owner/dataset",
            "apply": False,
            "progress": context.progress,
            "index": 2,
            "total": 3,
            "allow_bundle_only": True,
            "published_source_names": None,
        }
    ]
    assert record_calls == [(source, context, False)]


def test_publish_source_defaults_language_changed_to_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.repo_id = "owner/dataset"
    context.apply = False
    context.progress = None
    current_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_source_upload_is_current_for_context",
        lambda **kwargs: current_calls.append(kwargs) or False,
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
    assert current_calls[0]["language_changed"] is False


def test_source_upload_current_for_context_rejects_changed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    manifest = {"public_shard_sha256": "a" * 64}
    context: Any = type("Context", (), {})()
    context.apply = True
    context.state = type("State", (), {"sources": {source.name: manifest}})()
    context.upload_checkpoint = {"sources": {source.name: {"polygon_sha256": "a" * 64}}}
    context.progress = None
    context.run_dir = tmp_path

    assert source_processing._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=1,
        migration_changed=False,
        needs_enrichment=False,
        language_changed=False,
    )
    for changed in ("migration_changed", "needs_enrichment", "language_changed"):
        flags = {
            "migration_changed": False,
            "needs_enrichment": False,
            "language_changed": False,
        }
        flags[changed] = True
        assert not source_processing._source_upload_is_current_for_context(
            source=source,
            context=context,
            index=1,
            total=1,
            **flags,
        )

    context.apply = False
    assert not source_processing._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=1,
        migration_changed=False,
        needs_enrichment=False,
        language_changed=False,
    )

    context.apply = True
    context.upload_checkpoint = {"sources": {source.name: {"polygon_sha256": "b" * 64}}}
    assert not source_processing._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=1,
        migration_changed=False,
        needs_enrichment=False,
        language_changed=False,
    )


@pytest.mark.parametrize(
    ("apply", "migration_changed", "needs_enrichment", "language_changed", "expected"),
    [
        (False, False, False, False, False),
        (True, False, False, False, True),
        (False, True, False, False, True),
        (False, False, True, False, True),
        (False, False, False, True, True),
    ],
)
def test_source_requires_publication_accounts_for_every_change(
    apply: bool,
    migration_changed: bool,
    needs_enrichment: bool,
    language_changed: bool,
    expected: bool,
) -> None:
    context: Any = type("Context", (), {"apply": apply})()
    assert (
        source_processing._source_requires_publication(
            context=context,
            migration_changed=migration_changed,
            needs_enrichment=needs_enrichment,
            language_changed=language_changed,
        )
        is expected
    )


def test_publication_helpers_keep_previous_sources_and_ignore_false_uploads(
    tmp_path: Path,
) -> None:
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.apply = True
    context.upload_checkpoint = {"sources": {"previous.osm.pbf": {}}}
    assert source_processing._published_source_names(context, source) == {
        "previous.osm.pbf",
        source.name,
    }

    checkpoint: Any = {"sources": {}}
    state: Any = type("State", (), {"sources": {source.name: {}}})()
    context.state = state
    context.upload_checkpoint = checkpoint
    source_processing._record_source_upload(source, context, uploaded=False)
    assert checkpoint["sources"] == {}

    state.sources[source.name] = {"public_shard_sha256": "a" * 64}
    source_processing._record_source_upload(source, context, uploaded=True)
    assert checkpoint["sources"] == {
        source.name: {"polygon_sha256": "a" * 64},
    }

    context.progress = None
    source_processing._progress(context.progress, "ignored")
    messages: list[str] = []
    source_processing._progress(messages.append, "kept")
    assert messages == ["kept"]
    assert source_processing._public_shard_path(tmp_path, source) == (
        tmp_path / "polygons" / "a.parquet"
    )


@pytest.mark.parametrize(
    ("manifest", "filename", "checkpoint", "expected"),
    [
        ({"public_shard_sha256": "a" * 64}, "a.osm.pbf", {"sources": {}}, False),
        ({"public_shard_sha256": "a" * 64}, "a.osm.pbf", {"sources": []}, False),
        ({"public_shard_sha256": "a" * 64}, "a.osm.pbf", {"sources": {"a.osm.pbf": []}}, False),
        (
            {"public_shard_sha256": "a" * 64},
            "a.osm.pbf",
            {"sources": {"a.osm.pbf": {"polygon_sha256": "b" * 64}}},
            False,
        ),
        (
            {"public_shard_sha256": "a" * 64},
            "a.osm.pbf",
            {"sources": {"a.osm.pbf": {"polygon_sha256": "a" * 64}}},
            True,
        ),
    ],
)
def test_source_upload_checkpoint_match_is_strict(
    manifest: Mapping[str, object],
    filename: str,
    checkpoint: Any,
    expected: bool,
) -> None:
    assert source_processing._source_upload_is_current(manifest, filename, checkpoint) is expected


@pytest.mark.parametrize(
    ("shard_changed", "upload_paths", "allow_bundle_only", "expected"),
    [
        (False, [Path("a.parquet")], False, False),
        (False, [Path("a.parquet")], True, True),
        (True, [], True, False),
        (True, [Path("a.parquet")], True, True),
    ],
)
def test_maybe_publish_enriched_shard_respects_upload_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shard_changed: bool,
    upload_paths: list[Path],
    allow_bundle_only: bool,
    expected: bool,
) -> None:
    source = Path("a.osm.pbf")
    plan = SimpleNamespace(
        shard_changed=shard_changed,
        upload_paths=upload_paths,
        shard_sha256="a" * 64,
        bundle_state={},
    )
    published_source_names = {"previous.osm.pbf", source.name}
    card_calls: list[tuple[Path, object]] = []
    preview_calls: list[tuple[Path, Path, bool]] = []
    upload_calls: list[tuple[Path, Path, str, object]] = []
    persist_calls: list[tuple[Path, Path, str, object]] = []
    progress_messages: list[str] = []

    def fake_build_card(run_dir: Path, *, source_names: object = None) -> None:
        card_calls.append((run_dir, source_names))

    def fake_preview(run_dir_value: Path, source_value: Path, *, dry_run: bool) -> Any:
        preview_calls.append((run_dir_value, source_value, dry_run))
        return plan

    def fake_upload(
        run_dir_value: Path,
        source_value: Path,
        repo_id_value: str,
        plan_value: object,
    ) -> None:
        upload_calls.append((run_dir_value, source_value, repo_id_value, plan_value))

    def fake_persist(
        run_dir_value: Path,
        source_value: Path,
        *,
        shard_sha256: str,
        bundle_state: object,
    ) -> None:
        persist_calls.append((run_dir_value, source_value, shard_sha256, bundle_state))

    monkeypatch.setattr(
        source_processing,
        "build_card",
        fake_build_card,
    )
    monkeypatch.setattr(
        source_processing,
        "incremental_publish_changed_shard",
        fake_preview,
    )
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        fake_upload,
    )
    monkeypatch.setattr(
        source_processing,
        "persist_successful_upload",
        fake_persist,
    )

    result = source_processing._maybe_publish_enriched_shard(
        run_dir=tmp_path,
        source=source,
        repo_id="owner/dataset",
        apply=True,
        progress=progress_messages.append,
        index=1,
        total=1,
        allow_bundle_only=allow_bundle_only,
        published_source_names=published_source_names,
    )

    assert result is expected
    assert card_calls == [(tmp_path, published_source_names)]
    assert preview_calls == [(tmp_path, source, True)]
    if expected:
        assert upload_calls == [(tmp_path, source, "owner/dataset", plan)]
        assert persist_calls == [(tmp_path, source, "a" * 64, {})]
        assert progress_messages == ["[1/1] Uploading enriched shard and recomputed card"]
    else:
        assert upload_calls == []
        assert persist_calls == []
        assert progress_messages == []


def test_maybe_publish_enriched_shard_defaults_to_bundle_only_uploads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = SimpleNamespace(
        shard_changed=False,
        upload_paths=[tmp_path / "README.md"],
        shard_sha256="a" * 64,
        bundle_state={},
    )
    uploads: list[object] = []
    monkeypatch.setattr(source_processing, "build_card", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        source_processing,
        "incremental_publish_changed_shard",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda *args, **_kwargs: uploads.append(args),
    )
    monkeypatch.setattr(
        source_processing,
        "persist_successful_upload",
        lambda *_args, **_kwargs: None,
    )

    assert source_processing._maybe_publish_enriched_shard(
        run_dir=tmp_path,
        source=Path("a.osm.pbf"),
        repo_id="owner/dataset",
        apply=True,
        progress=None,
        index=1,
        total=1,
    )
    assert uploads


def test_upload_public_shard_uses_supplied_incremental_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    plan = IncrementalPublishPlan(
        source_filename=source.name,
        upload_paths=[tmp_path / "a.parquet"],
        shard_changed=True,
        bundle_changed=False,
        shard_sha256="a" * 64,
        bundle_state={},
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_upload_folder",
        lambda run_dir, **kwargs: calls.append({"run_dir": run_dir, **kwargs}),
    )

    source_processing._upload_public_shard(tmp_path, source, "owner/dataset", plan)

    assert calls == [
        {
            "run_dir": tmp_path,
            "repo_id": "owner/dataset",
            "repo_kind": "dataset",
            "artifact_paths": plan.upload_paths,
        }
    ]
    with pytest.raises(ValueError) as error:
        source_processing._upload_public_shard(tmp_path, Path("b.osm.pbf"), "owner/dataset", plan)
    assert str(error.value) == "incremental publish plan does not match source"


def test_upload_public_shard_uploads_the_complete_bundle_without_a_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_upload_folder",
        lambda run_dir, **kwargs: calls.append({"run_dir": run_dir, **kwargs}),
    )

    source_processing._upload_public_shard(tmp_path, source, "owner/dataset")

    assert calls == [
        {
            "run_dir": tmp_path,
            "repo_id": "owner/dataset",
            "repo_kind": "dataset",
            "artifact_paths": [
                tmp_path / "polygons" / "a.parquet",
                tmp_path / "README.md",
                tmp_path / "dataset.yaml",
            ],
        }
    ]


def test_upload_public_shard_delegates_to_incremental_uploader_when_map_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    map_path = tmp_path / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"map")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "incremental_publish_changed_shard",
        lambda *args, **kwargs: calls.append({"args": args, **kwargs}) or object(),
    )

    source_processing._upload_public_shard(tmp_path, source, "owner/dataset")

    assert calls == [
        {
            "args": (tmp_path, source),
            "repo_id": "owner/dataset",
            "repo_kind": "dataset",
            "dry_run": False,
            "uploader": source_processing._upload_folder,
        }
    ]


def test_schema_enrichment_checks_legacy_and_current_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parquet: Any = type("Parquet", (), {"schema_arrow": object()})()
    calls: list[tuple[object, object]] = []

    def matches(schema: object, expected: object) -> bool:
        calls.append((schema, expected))
        return expected is POLYGON_PUBLIC_SCHEMA_V1_1

    monkeypatch.setattr(source_processing, "schema_matches", matches)
    assert source_processing._schema_needs_enrichment(parquet)
    assert calls == [(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_1)]

    calls.clear()

    def current_matches(schema: object, expected: object) -> bool:
        calls.append((schema, expected))
        return expected is POLYGON_PUBLIC_SCHEMA

    monkeypatch.setattr(source_processing, "schema_matches", current_matches)
    assert not source_processing._schema_needs_enrichment(parquet)
    assert calls == [
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_1),
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA),
    ]

    calls.clear()

    def unknown_matches(schema: object, expected: object) -> bool:
        calls.append((schema, expected))
        return False

    monkeypatch.setattr(source_processing, "schema_matches", unknown_matches)
    assert source_processing._schema_needs_enrichment(parquet)
    assert calls == [
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_1),
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA),
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_4),
    ]


def test_status_columns_stop_at_the_first_retryable_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Batch:
        def column(self, name: str) -> object:
            return name

    class Parquet:
        def iter_batches(self, **kwargs: object):  # type: ignore[no-untyped-def]
            assert kwargs == {
                "columns": ["website_text_status", "contact_website_text_status"],
                "batch_size": 8_192,
            }
            yield Batch()

    values = {"website_text_status": False, "contact_website_text_status": True}
    calls: list[object] = []
    monkeypatch.setattr(
        source_processing,
        "status_has_retryable_value",
        lambda value: calls.append(value) or values[value],
    )

    assert source_processing._status_columns_need_enrichment(cast(Any, Parquet()))
    assert calls == ["website_text_status", "contact_website_text_status"]


def test_status_columns_report_complete_when_no_column_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Batch:
        def column(self, name: str) -> str:
            return name

    class Parquet:
        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield Batch()

    monkeypatch.setattr(source_processing, "status_has_retryable_value", lambda _value: False)

    assert not source_processing._status_columns_need_enrichment(cast(Any, Parquet()))


def test_shard_needs_enrichment_scans_schema_then_status_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Parquet:
        schema_arrow = object()

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            raise AssertionError("schema requiring enrichment must stop before status scan")

    opened: list[Path] = []

    def open_parquet(path: Path) -> Parquet:
        opened.append(path)
        return Parquet()

    monkeypatch.setattr(source_processing.pq, "ParquetFile", open_parquet)
    monkeypatch.setattr(source_processing, "_schema_needs_enrichment", lambda _parquet: True)
    monkeypatch.setattr(
        source_processing,
        "_status_columns_need_enrichment",
        lambda _parquet: pytest.fail("status scan should be skipped"),
    )
    assert source_processing._shard_needs_enrichment(tmp_path / "a.parquet")
    assert opened == [tmp_path / "a.parquet"]


def test_run_needs_enrichment_scans_sorted_public_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    first = polygons / "a.parquet"
    second = polygons / "b.parquet"
    first.touch()
    second.touch()
    (polygons / "ignored.PARQUET").touch()
    seen: list[Path] = []

    def needs_enrichment(shard: Path) -> bool:
        seen.append(shard)
        return shard == second

    monkeypatch.setattr(source_processing, "_shard_needs_enrichment", needs_enrichment)

    assert source_processing._run_needs_enrichment(tmp_path)
    assert seen == [first, second]


def test_run_needs_language_detection_scans_sorted_public_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    first = polygons / "a.parquet"
    second = polygons / "b.parquet"
    first.touch()
    second.touch()
    (polygons / "ignored.PARQUET").touch()
    seen: list[Path] = []

    def needs_detection(shard: Path) -> bool:
        seen.append(shard)
        return shard == second

    monkeypatch.setattr(source_processing, "shard_needs_language_detection", needs_detection)

    assert source_processing._run_needs_language_detection(tmp_path)
    assert seen == [first, second]


def test_run_needs_language_detection_returns_false_when_every_shard_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    shard = polygons / "a.parquet"
    shard.touch()
    seen: list[Path] = []

    def needs_detection(path: Path) -> bool:
        seen.append(path)
        return False

    monkeypatch.setattr(source_processing, "shard_needs_language_detection", needs_detection)

    assert not source_processing._run_needs_language_detection(tmp_path)
    assert seen == [shard]
