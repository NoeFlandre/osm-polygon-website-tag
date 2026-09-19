"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.contracts.text_schema import count_words
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
from osm_polygon_website_tag.reporting.finalize import FinalizationReport
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.verify import VerificationReport
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_INITIALIZED,
    RunState,
    SourceFingerprint,
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


def _noop_progress(_message: str) -> None:
    return None


class RecordingLanguageDetector:
    """Small deterministic detector for workflow tests."""

    identity = ModelIdentity("repo", "model.bin", "revision", "a" * 64)

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        self.calls.append(list(texts))
        return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]


class InterruptingLanguageDetector(RecordingLanguageDetector):
    """Detector that interrupts after a selected prediction call."""

    def __init__(self, *, interrupt_on_call: int) -> None:
        super().__init__()
        self.interrupt_on_call = interrupt_on_call

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        result = super().predict(texts)
        if len(self.calls) == self.interrupt_on_call:
            raise KeyboardInterrupt
        return result


@pytest.fixture(autouse=True)
def _offline_remote_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep workflow tests local; remote reconciliation has dedicated unit tests."""
    from osm_polygon_website_tag.publishing.incremental import load_upload_checkpoint

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.reconcile_upload_checkpoint",
        lambda run_dir, **_kwargs: load_upload_checkpoint(run_dir),
    )


def _write_card_contract_fixture(run_dir: Path, receipt: object) -> None:
    map_path = run_dir / POLYGON_DENSITY_ASSET_REL_PATH
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"map")
    (run_dir / "stats.json").write_text("stats")
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt))


def _checkpoint() -> CheckpointV2:
    return {"schema_version": "v2", "global_bundle": {}, "sources": {}}


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

    monkeypatch.setattr(source_processing, "enrich_polygon_shard", enrich, raising=False)


def test_prepare_workflow_setup_forwards_progress_and_builds_the_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "sources"
    output_root = tmp_path / "runs"
    requested_run_dir = output_root / "run"
    source = source_root / "source.osm.pbf"
    fingerprint = SourceFingerprint(source.name, 1, 2)
    state = RunState(requested_run_dir, "run", metadata={"status": STATUS_INITIALIZED})
    progress = _noop_progress
    calls: list[object] = []

    monkeypatch.setattr(
        workflow, "discover_sources", lambda root: [source] if root == source_root else []
    )
    monkeypatch.setattr(workflow, "snapshot_source_fingerprint", lambda path: fingerprint)

    def load_or_initialise(**kwargs: object) -> tuple[Path, RunState]:
        calls.append(kwargs)
        return requested_run_dir, state

    monkeypatch.setattr(workflow, "_load_or_initialise_state", load_or_initialise)
    monkeypatch.setattr(workflow, "_validated_status", lambda raw: "validated")
    monkeypatch.setattr(workflow, "_reopen_snapshot_if_needed", lambda value: value)

    def refresh(**kwargs: object) -> tuple[RunState, str]:
        calls.append(kwargs)
        return state, "refreshed"

    monkeypatch.setattr(workflow, "_refresh_legacy_card_if_needed", refresh)

    result = workflow._prepare_workflow_setup(
        source_root=source_root,
        output_root=output_root,
        run_id="run",
        run_dir=requested_run_dir,
        existing_state=None,
        progress=progress,
    )

    assert calls == [
        {
            "output_root": output_root,
            "run_id": "run",
            "run_dir": requested_run_dir,
            "fingerprints": [fingerprint],
            "source_root": source_root,
            "existing_state": None,
        },
        {"run_dir": requested_run_dir, "state": state, "status": "validated", "progress": progress},
    ]
    assert result == workflow._WorkflowSetup(
        requested_run_dir,
        state,
        [source],
        {source.name: fingerprint},
        "refreshed",
    )


def test_load_or_initialise_state_reports_the_exact_inventory_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = RunState(tmp_path / "run", "run", metadata={"status": STATUS_COMPLETE})
    fingerprint = SourceFingerprint("source.osm.pbf", 1, 2)
    monkeypatch.setattr(workflow, "expected_source_inventory", lambda _run_dir: [fingerprint])
    monkeypatch.setattr(workflow, "source_inventory_matches_expected", lambda *_args: False)

    with pytest.raises(
        ValueError, match=r"^source inventory changed since this run was initialized$"
    ):
        workflow._load_or_initialise_state(
            output_root=tmp_path,
            run_id="run",
            run_dir=existing.run_dir,
            fingerprints=[fingerprint],
            source_root=tmp_path / "sources",
            existing_state=existing,
        )


@pytest.mark.parametrize(
    ("snapshot_status", "expected_calls"),
    [("done", [("snapshot_status", "in_progress")]), ("in_progress", [])],
)
def test_reopen_snapshot_only_reopens_a_done_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_status: str,
    expected_calls: list[tuple[str, str]],
) -> None:
    state = RunState(
        tmp_path / "run",
        "run",
        metadata={"snapshot_status": snapshot_status},
    )
    calls: list[tuple[RunState, dict[str, str]]] = []
    monkeypatch.setattr(
        workflow,
        "upsert_run_metadata",
        lambda state_value, patch: calls.append((state_value, patch)),
    )

    assert workflow._reopen_snapshot_if_needed(state) is state
    assert [(key, patch[key]) for _state, patch in calls for key in patch] == expected_calls
    assert all(state_value is state for state_value, _patch in calls)


def test_refresh_legacy_card_forwards_progress_and_reloads_the_refreshed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = RunState(tmp_path / "run", "run", metadata={"status": STATUS_COMPLETE})
    refreshed_state = RunState(tmp_path / "run", "run", metadata={"status": STATUS_COMPLETE})
    progress: list[str] = []
    calls: list[Path] = []
    monkeypatch.setattr(workflow, "_card_refresh_needed", lambda _run_dir: True)
    monkeypatch.setattr(
        workflow,
        "refresh_card_run",
        lambda run_dir: (
            calls.append(run_dir) or FinalizationReport(True, {}, VerificationReport(True))
        ),
    )
    monkeypatch.setattr(workflow, "load_run", lambda run_dir: refreshed_state)

    result = workflow._refresh_legacy_card_if_needed(
        run_dir=state.run_dir,
        state=state,
        status=STATUS_COMPLETE,
        progress=progress.append,
    )

    assert result == (refreshed_state, STATUS_COMPLETE)
    assert calls == [state.run_dir]
    assert progress == ["Refreshing the legacy dataset card and H3 density map"]


def test_prepare_upload_checkpoint_forwards_all_upload_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = _checkpoint()
    progress = _noop_progress
    calls: list[object] = []

    def require_token(apply: bool) -> str:
        calls.append(("token", apply))
        return "token"

    def ensure_repo(repo_id: str, **kwargs: object) -> None:
        calls.append(("repo", repo_id, kwargs))

    def load_checkpoint(run_dir: Path) -> CheckpointV2:
        calls.append(("load", run_dir))
        return checkpoint

    def reconcile(**kwargs: object) -> CheckpointV2:
        calls.append(("reconcile", kwargs))
        return checkpoint

    monkeypatch.setattr(workflow, "_require_upload_token", require_token)
    monkeypatch.setattr(workflow, "_ensure_dataset_repo", ensure_repo)
    monkeypatch.setattr(workflow, "load_upload_checkpoint", load_checkpoint)
    monkeypatch.setattr(workflow, "_reconcile_checkpoint", reconcile)

    result = workflow._prepare_upload_checkpoint(
        run_dir=tmp_path,
        repo_id="owner/dataset",
        apply=True,
        ensure_repo=True,
        progress=progress,
    )

    assert result is checkpoint
    assert calls == [
        ("token", True),
        ("repo", "owner/dataset", {"apply": True, "ensure_repo": True, "progress": progress}),
        ("load", tmp_path),
        (
            "reconcile",
            {
                "run_dir": tmp_path,
                "repo_id": "owner/dataset",
                "token": "token",
                "checkpoint": checkpoint,
                "apply": True,
            },
        ),
    ]


def test_reconcile_checkpoint_requires_apply_credentials_and_preserves_dry_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = _checkpoint()
    calls: list[object] = []
    monkeypatch.setattr(
        workflow,
        "reconcile_upload_checkpoint",
        lambda run_dir, *, repo_id, token: calls.append((run_dir, repo_id, token)) or checkpoint,
    )

    assert (
        workflow._reconcile_checkpoint(
            run_dir=tmp_path,
            repo_id="owner/dataset",
            token=None,
            checkpoint=checkpoint,
            apply=False,
        )
        is checkpoint
    )
    assert calls == []
    assert (
        workflow._reconcile_checkpoint(
            run_dir=tmp_path,
            repo_id="owner/dataset",
            token="token",
            checkpoint=checkpoint,
            apply=True,
        )
        is checkpoint
    )
    assert calls == [(tmp_path, "owner/dataset", "token")]
    with pytest.raises(ValueError, match=r"^apply mode requires Hugging Face credentials$"):
        workflow._reconcile_checkpoint(
            run_dir=tmp_path,
            repo_id="owner/dataset",
            token=None,
            checkpoint=checkpoint,
            apply=True,
        )


def test_dry_run_resume_names_separates_completed_and_pending_sources(tmp_path: Path) -> None:
    state = RunState(
        tmp_path,
        "run",
        sources={
            "done.osm.pbf": {
                "filename": "done.osm.pbf",
                "size_bytes": 1,
                "mtime_ns": 2,
                "enrichment_pending": False,
            },
            "pending.osm.pbf": {
                "filename": "pending.osm.pbf",
                "size_bytes": 1,
                "mtime_ns": 2,
                "enrichment_pending": True,
            },
            "legacy.osm.pbf": {"filename": "legacy.osm.pbf", "size_bytes": 1, "mtime_ns": 2},
        },
    )

    assert workflow._dry_run_resume_names(state) == (
        {"done.osm.pbf"},
        {"pending.osm.pbf", "legacy.osm.pbf"},
    )


def test_run_enrichment_phase_disables_extraction_and_transitions_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [Path("source.osm.pbf")]
    ordered_sources = [Path("source.osm.pbf")]
    fingerprints: dict[str, SourceFingerprint] = {}
    state = object()
    context = cast(Any, type("Context", (), {"state": state})())
    counts = source_processing.SourcePhaseCounts(extracted=1, reused=2, uploaded=3)
    calls: list[object] = []
    monkeypatch.setattr(
        workflow,
        "process_sources",
        lambda **kwargs: calls.append(kwargs) or counts,
    )
    monkeypatch.setattr(
        workflow,
        "transition_status",
        lambda state, status: calls.append((state, status)),
    )

    status, result = workflow._run_enrichment_phase(
        sources,
        ordered_sources,
        fingerprints,
        context,
    )

    assert status == STATUS_ENRICHED
    assert result is counts
    assert calls == [
        {
            "sources": sources,
            "ordered_sources": ordered_sources,
            "fingerprints_by_name": fingerprints,
            "context": context,
            "allow_extraction": False,
        },
        (state, STATUS_ENRICHED),
    ]


def test_add_phase_counts_adds_each_counter() -> None:
    left = source_processing.SourcePhaseCounts(extracted=1, reused=2, uploaded=3)
    right = source_processing.SourcePhaseCounts(extracted=4, reused=5, uploaded=6)

    assert workflow._add_phase_counts(left, right) == source_processing.SourcePhaseCounts(5, 7, 9)


def test_build_analysis_forwards_progress_and_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = object()
    progress: list[str] = []
    calls: list[object] = []
    context = cast(
        Any,
        type("Context", (), {"run_dir": tmp_path, "state": state, "progress": progress.append})(),
    )
    monkeypatch.setattr(
        workflow, "analyze_results", lambda run_dir: calls.append(("analyze", run_dir))
    )
    monkeypatch.setattr(
        workflow,
        "transition_status",
        lambda state_value, status: calls.append(("transition", state_value, status)),
    )

    assert workflow._build_analysis_if_needed(STATUS_ENRICHED, context) == STATUS_ANALYZED
    assert calls == [("analyze", tmp_path), ("transition", state, STATUS_ANALYZED)]
    assert progress == ["Building aggregate analysis"]


def test_build_card_forwards_progress_and_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = object()
    progress: list[str] = []
    calls: list[object] = []
    context = cast(
        Any,
        type("Context", (), {"run_dir": tmp_path, "state": state, "progress": progress.append})(),
    )
    monkeypatch.setattr(workflow, "build_card", lambda run_dir: calls.append(("card", run_dir)))
    monkeypatch.setattr(
        workflow,
        "transition_status",
        lambda state_value, status: calls.append(("transition", state_value, status)),
    )

    assert workflow._build_card_if_needed(STATUS_ANALYZED, context) == STATUS_CARD_BUILT
    assert calls == [("card", tmp_path), ("transition", state, STATUS_CARD_BUILT)]
    assert progress == ["Building artifact-derived dataset card"]
