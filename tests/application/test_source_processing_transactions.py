from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.application.source_processing import SourceProcessingContext
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
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
