"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import project_current_rows_to_legacy

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.application.inventory import (
    discover_sources as inventory_discover_sources,
)
from osm_polygon_website_tag.application.source_processing import SourceProcessingContext
from osm_polygon_website_tag.application.workflow import (
    discover_sources,
    run_all,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
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
    STATUS_ENRICHING,
    STATUS_EXTRACTING,
    STATUS_INITIALIZED,
    RunState,
    SourceFingerprint,
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


def test_workflow_preserves_discover_sources_compatibility_import() -> None:
    assert discover_sources is inventory_discover_sources
    from osm_polygon_website_tag.application import resume_planner, workflow

    assert workflow.prioritize_sources is resume_planner.prioritize_sources


def test_shard_needs_enrichment_scans_status_columns_without_row_dicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume checks should inspect Arrow columns, not materialize every row."""

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

    monkeypatch.setattr(source_processing.pq, "ParquetFile", lambda _path: FakeParquet())

    assert source_processing._shard_needs_enrichment(tmp_path / "source.parquet") is True


@pytest.mark.parametrize(
    ("needs_enrichment", "detect_languages", "needs_language", "expected"),
    [
        (False, False, False, False),
        (True, False, False, True),
        (False, True, False, False),
        (False, True, True, True),
        (True, True, False, True),
    ],
)
def test_run_requires_enrichment_combines_source_and_language_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    needs_enrichment: bool,
    detect_languages: bool,
    needs_language: bool,
    expected: bool,
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        workflow,
        "_run_needs_enrichment",
        lambda run_dir: calls.append(run_dir) or needs_enrichment,
    )
    monkeypatch.setattr(
        workflow,
        "_run_needs_language_detection",
        lambda run_dir: calls.append(run_dir) or needs_language,
    )
    context = type("Context", (), {"run_dir": tmp_path, "detect_languages": detect_languages})()

    assert workflow._run_requires_enrichment(cast(Any, context)) is expected
    assert calls == [tmp_path] * (2 if not needs_enrichment and detect_languages else 1)


def test_transition_to_enriching_updates_the_workflow_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = object()
    context = type("Context", (), {"state": state})()
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        workflow,
        "transition_status",
        lambda state_value, status: calls.append((state_value, status)),
    )

    assert workflow._transition_to_enriching(cast(Any, context)) == STATUS_ENRICHING
    assert calls == [(state, STATUS_ENRICHING)]


def _write_card_contract_fixture(run_dir: Path, receipt: object) -> None:
    map_path = run_dir / POLYGON_DENSITY_ASSET_REL_PATH
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"map")
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt))


@pytest.mark.parametrize(
    "receipt",
    [
        {"card_contract_version": 2},
        {"other": "value"},
        ["not", "a", "mapping"],
        "not a mapping",
    ],
)
def test_card_refresh_needed_rejects_every_non_current_receipt(
    tmp_path: Path,
    receipt: object,
) -> None:
    _write_card_contract_fixture(tmp_path, receipt)
    assert workflow._card_refresh_needed(tmp_path)


def test_card_refresh_needed_accepts_current_receipt(
    tmp_path: Path,
) -> None:
    _write_card_contract_fixture(tmp_path, {"card_contract_version": 1})
    assert not workflow._card_refresh_needed(tmp_path)


def test_card_refresh_needed_requires_the_map_and_readable_receipt(tmp_path: Path) -> None:
    assert workflow._card_refresh_needed(tmp_path)
    _write_card_contract_fixture(tmp_path, {"card_contract_version": 1})
    (tmp_path / "manifests" / "completion_receipt.json").unlink()
    assert workflow._card_refresh_needed(tmp_path)
    receipt_path = tmp_path / "manifests" / "completion_receipt.json"
    receipt_path.write_text("{invalid json")
    assert workflow._card_refresh_needed(tmp_path)


def test_card_refresh_needed_uses_the_exact_contract_paths_and_encoding() -> None:
    class PathSpy:
        def __init__(self, parts: tuple[str, ...] = ()) -> None:
            self.parts = parts

        def __truediv__(self, part: object) -> PathSpy:
            return PathSpy((*self.parts, str(part)))

        def is_file(self) -> bool:
            return self.parts == ("assets/geographic_polygon_density.png",)

        def read_text(self, *, encoding: str) -> str:
            assert self.parts == ("manifests", "completion_receipt.json")
            assert encoding == "utf-8"
            return '{"card_contract_version": 1}'

    assert not workflow._card_refresh_needed(cast(Any, PathSpy()))


def _checkpoint() -> CheckpointV2:
    return {"schema_version": "v2", "global_bundle": {}, "sources": {}}


def test_run_all_forwards_each_orchestration_boundary_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "sources"
    output_root = tmp_path / "runs"
    run_dir = output_root / "refactor"
    source = source_root / "source.osm.pbf"
    sources = [source]
    fingerprint = SourceFingerprint(source.name, 1, 2)
    state = RunState(run_dir=run_dir, run_id="refactor", metadata={"status": STATUS_INITIALIZED})
    setup = workflow._WorkflowSetup(
        run_dir=run_dir,
        state=state,
        sources=sources,
        fingerprints_by_name={source.name: fingerprint},
        status=STATUS_INITIALIZED,
    )
    checkpoint = _checkpoint()
    detector = RecordingLanguageDetector()
    progress = _noop_progress
    calls: dict[str, object] = {}

    def prepare_setup(**kwargs: object) -> workflow._WorkflowSetup:
        calls["setup"] = kwargs
        return setup

    def prepare_detector(**kwargs: object) -> RecordingLanguageDetector:
        calls["detector"] = kwargs
        return detector

    def prepare_checkpoint(**kwargs: object) -> CheckpointV2:
        calls["checkpoint"] = kwargs
        return checkpoint

    def resume_names(
        state_value: RunState,
        checkpoint_value: CheckpointV2,
        *,
        apply: bool,
    ) -> tuple[set[str], set[str]]:
        calls["resume"] = (state_value, checkpoint_value, apply)
        return {source.name}, set()

    def prepare_priorities(
        run_dir_value: Path,
        state_value: RunState,
        sources_value: list[Path],
        *,
        retry_names: set[str],
    ) -> tuple[set[str], dict[str, tuple[int, int]]]:
        calls["partial"] = (run_dir_value, state_value, sources_value, retry_names)
        return {source.name}, {source.name: (1, -1)}

    def prioritize(
        sources_value: list[Path],
        processed_names: set[str],
        *,
        retry_names: set[str],
        partial_names: set[str],
        retry_priorities: dict[str, tuple[int, int]],
    ) -> list[Path]:
        calls["prioritize"] = (
            sources_value,
            processed_names,
            retry_names,
            partial_names,
            retry_priorities,
        )
        return sources_value

    counts = source_processing.SourcePhaseCounts(extracted=4, reused=5, uploaded=6)

    def run_phases(
        status: str,
        source_values: list[Path],
        ordered_values: list[Path],
        fingerprints: dict[str, SourceFingerprint],
        context: SourceProcessingContext,
    ) -> tuple[str, source_processing.SourcePhaseCounts]:
        calls["phases"] = (status, source_values, ordered_values, fingerprints, context)
        return "finished", counts

    def complete(status: str, context: SourceProcessingContext) -> str:
        calls["complete"] = (status, context)
        return STATUS_COMPLETE

    monkeypatch.setattr(workflow, "_prepare_workflow_setup", prepare_setup)
    monkeypatch.setattr(workflow, "_prepare_language_detector", prepare_detector)
    monkeypatch.setattr(workflow, "_prepare_upload_checkpoint", prepare_checkpoint)
    monkeypatch.setattr(workflow, "_resume_source_names", resume_names)
    monkeypatch.setattr(workflow, "prepare_resume_priorities", prepare_priorities)
    monkeypatch.setattr(workflow, "prioritize_sources", prioritize)
    monkeypatch.setattr(workflow, "_run_source_phases", run_phases)
    monkeypatch.setattr(workflow, "_complete_workflow", complete)

    result = run_all(
        source_root=source_root,
        output_root=output_root,
        run_id="refactor",
        repo_id="owner/dataset",
        apply=True,
        ensure_repo=True,
        progress=progress,
        area_workers=3,
        max_in_flight_areas=4,
        fetch_workers=5,
        detect_languages=True,
        language_detector=detector,
    )

    assert calls["setup"] == {
        "source_root": source_root.resolve(),
        "output_root": output_root.resolve(),
        "run_id": "refactor",
        "run_dir": run_dir.resolve(),
        "existing_state": None,
        "progress": progress,
    }
    assert calls["detector"] == {
        "detect_languages": True,
        "language_detector": detector,
        "run_dir": run_dir,
    }
    assert calls["checkpoint"] == {
        "run_dir": run_dir,
        "repo_id": "owner/dataset",
        "apply": True,
        "ensure_repo": True,
        "progress": progress,
    }
    assert calls["resume"] == (state, checkpoint, True)
    assert calls["partial"] == (run_dir, state, sources, set())
    assert calls["prioritize"] == (
        sources,
        {source.name},
        set(),
        {source.name},
        {source.name: (1, -1)},
    )
    phase_status, phase_sources, ordered, fingerprints, context = cast(
        tuple[str, list[Path], list[Path], dict[str, SourceFingerprint], SourceProcessingContext],
        calls["phases"],
    )
    assert (phase_status, phase_sources, ordered, fingerprints) == (
        STATUS_INITIALIZED,
        sources,
        sources,
        {source.name: fingerprint},
    )
    assert context.run_dir == run_dir
    assert context.state is state
    assert context.repo_id == "owner/dataset"
    assert context.apply is True
    assert context.progress is progress
    assert context.area_workers == 3
    assert context.max_in_flight_areas == 4
    assert context.fetch_workers == 5
    assert context.detect_languages is True
    assert context.language_detector is detector
    assert calls["complete"] == ("finished", context)
    assert result.run_dir == run_dir
    assert result.source_count == 1
    assert result.extracted_count == 4
    assert result.skipped_count == 5
    assert result.uploaded_count == 6
    assert result.complete is True
    assert result.dry_run is False


def test_frozen_snapshot_result_returns_the_exact_immutable_summary(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    receipt = run_dir / "manifests" / "completion_receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}")
    state = RunState(
        run_dir=run_dir,
        run_id="run",
        metadata={"status": STATUS_COMPLETE, "snapshot_status": "done"},
        sources={"source.osm.pbf": {"filename": "source.osm.pbf", "size_bytes": 1, "mtime_ns": 2}},
    )
    progress: list[str] = []

    result = workflow._frozen_snapshot_result(run_dir, state, apply=True, progress=progress.append)

    assert result == workflow.WorkflowResult(run_dir, 1, 0, 0, 0, True, False)
    assert progress == ["Frozen snapshot is already complete; skipping enrichment and uploads"]


def test_frozen_snapshot_result_uses_the_exact_receipt_path() -> None:
    class PathSpy:
        def __init__(self, parts: tuple[str, ...] = ()) -> None:
            self.parts = parts

        def __truediv__(self, part: object) -> PathSpy:
            return PathSpy((*self.parts, str(part)))

        def is_file(self) -> bool:
            assert self.parts == ("manifests", "completion_receipt.json")
            return True

    run_dir = PathSpy()
    state = RunState(
        run_dir=Path("/run"),
        run_id="run",
        metadata={"status": STATUS_COMPLETE, "snapshot_status": "done"},
        sources={
            "source.osm.pbf": {
                "filename": "source.osm.pbf",
                "size_bytes": 1,
                "mtime_ns": 2,
            }
        },
    )

    result = workflow._frozen_snapshot_result(cast(Any, run_dir), state, True, None)

    assert result is not None
    assert result.run_dir is run_dir


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


def test_finalize_forwards_progress_and_requires_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress: list[str] = []
    calls: list[Path] = []
    context = cast(
        Any,
        type("Context", (), {"run_dir": tmp_path, "progress": progress.append})(),
    )
    monkeypatch.setattr(
        workflow,
        "finalize_run",
        lambda run_dir: (
            calls.append(run_dir) or FinalizationReport(True, {}, VerificationReport(True))
        ),
    )

    assert workflow._finalize_if_needed(STATUS_CARD_BUILT, context) == STATUS_COMPLETE
    assert calls == [tmp_path]
    assert progress == ["Verifying and finalizing the complete run"]


def test_publish_complete_run_forwards_receipt_upload_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress: list[str] = []
    calls: list[tuple[Path, str, bool]] = []
    context = cast(
        Any,
        type(
            "Context",
            (),
            {
                "run_dir": tmp_path,
                "repo_id": "owner/dataset",
                "apply": True,
                "progress": progress.append,
            },
        )(),
    )
    monkeypatch.setattr(
        workflow,
        "publish_to_hf",
        lambda run_dir, *, repo_id, dry_run: calls.append((run_dir, repo_id, dry_run)),
    )

    workflow._publish_complete_run(STATUS_COMPLETE, context)

    assert calls == [(tmp_path, "owner/dataset", False)]
    assert progress == ["Uploading the receipt-bound complete dataset"]


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
        "osm_polygon_website_tag.application.source_processing._upload_public_shard",
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


def test_run_all_default_does_not_load_language_model(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    monkeypatch.setattr(
        workflow,
        "load_glotlid_detector",
        lambda *_args, **_kwargs: pytest.fail("default run-all must not load GlotLID"),
        raising=False,
    )

    result = run_all(
        source_root=_sources(make_pbf, tmp_path),
        output_root=tmp_path / "runs",
        run_id="plain",
    )

    assert result.complete
    assert all(
        pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
        for path in (result.run_dir / "polygons").glob("*.parquet")
    )


def test_run_all_opt_in_detects_and_publishes_language_shards(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    detector = RecordingLanguageDetector()
    uploads: list[str] = []
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda _run, source, _repo, *_args: uploads.append(source.name),
    )
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    result = run_all(
        source_root=_sources(make_pbf, tmp_path),
        output_root=tmp_path / "runs",
        run_id="language",
        apply=True,
        detect_languages=True,
        language_detector=detector,
    )

    assert result.complete
    assert detector.calls == [["text from https://example.org"]]
    assert uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    for path in (result.run_dir / "polygons").glob("*.parquet"):
        assert pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
        assert load_run(result.run_dir).sources[path.stem + ".osm.pbf"]["public_shard_sha256"] == (
            hash_shard(path)
        )


def test_run_all_opt_in_does_not_downgrade_existing_v1_4_shards(
    make_pbf,
    tmp_path: Path,
) -> None:
    root = _sources(make_pbf, tmp_path)
    first = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="language",
        detect_languages=True,
        language_detector=RecordingLanguageDetector(),
    )
    detector = RecordingLanguageDetector()

    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="language",
        detect_languages=True,
        language_detector=detector,
    )

    assert resumed.complete
    assert detector.calls == []
    assert all(
        pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
        for path in (first.run_dir / "polygons").glob("*.parquet")
    )


def test_run_all_language_detection_resumes_after_an_interrupted_shard(
    make_pbf,
    tmp_path: Path,
) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    for name in ("a-latest.osm.pbf", "b-latest.osm.pbf"):
        generated = make_pbf(_WEBSITE_OSM, name=name)
        (root / name).write_bytes((generated / name).read_bytes())

    with pytest.raises(KeyboardInterrupt):
        run_all(
            source_root=root,
            output_root=tmp_path / "runs",
            run_id="language",
            detect_languages=True,
            language_detector=InterruptingLanguageDetector(interrupt_on_call=2),
        )

    resumed_detector = RecordingLanguageDetector()
    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="language",
        detect_languages=True,
        language_detector=resumed_detector,
    )

    assert result.complete
    assert resumed_detector.calls == [["text from https://example.org"]]
    assert all(
        pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
        for path in (result.run_dir / "polygons").glob("*.parquet")
    )


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
        "osm_polygon_website_tag.application.source_processing.extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("legacy card refresh must not read PBFs"),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing.enrich_polygon_shard",
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
        source_processing,
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
    root = _sources(make_pbf, tmp_path)
    original = source_processing.extract_pbf
    calls = 0

    def interrupt_second(source: Path, run_dir: Path, run_state=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(source, run_dir, run_state=run_state)

    monkeypatch.setattr(source_processing, "extract_pbf", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    run_dir = tmp_path / "runs" / "production"
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING

    monkeypatch.setattr(source_processing, "extract_pbf", original)
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
    from osm_polygon_website_tag.application import workflow

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
    source_processing.extract_pbf(sources[0], run_dir, run_state=state)

    extracted_on_resume: list[str] = []
    original_extract = source_processing.extract_pbf

    def track_extract(source, *args, **kwargs):
        extracted_on_resume.append(Path(source).name)
        return original_extract(source, *args, **kwargs)

    monkeypatch.setattr(source_processing, "extract_pbf", track_extract)
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

    extracted_on_resume: list[str] = []
    original_extract = source_processing.extract_pbf

    def track_extract(source, *args, **kwargs):
        extracted_on_resume.append(Path(source).name)
        return original_extract(source, *args, **kwargs)

    monkeypatch.setattr(source_processing, "extract_pbf", track_extract)
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
    from osm_polygon_website_tag.application import workflow

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
    captured: list[Path] = []

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    source_processing._upload_public_shard(run_dir, Path("source.osm.pbf"), "owner/dataset")

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
        "osm_polygon_website_tag.application.source_processing._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    source_processing._upload_public_shard(run_dir, Path("source.osm.pbf"), "owner/dataset")

    assert captured == [shard, run_dir / "README.md", run_dir / "dataset.yaml", map_path]


def test_resume_enriches_only_shards_with_retryable_text(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    original = source_processing.enrich_polygon_shard
    enriched: list[str] = []
    bundle_checks: list[str] = []

    original_bundle_check = source_processing.source_bundle_is_complete

    def track_bundle_check(run_dir, manifest, fingerprint):
        bundle_checks.append(fingerprint.filename)
        return original_bundle_check(run_dir, manifest, fingerprint)

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", track_bundle_check)

    def track(shard, **kwargs):
        enriched.append(Path(shard).name)
        return original(shard, **kwargs)

    monkeypatch.setattr(source_processing, "enrich_polygon_shard", track)

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

    def upload_shard(_run_dir, source, _repo_id, *_args):
        shard_uploads.append(source.name)
        # Raise KeyboardInterrupt only once: on the first attempt to upload
        # source "b". The resume invocation should complete normally.
        if source.name == "b-latest.osm.pbf" and not interrupted["done"]:
            interrupted["done"] = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing._upload_public_shard", upload_shard
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
    resumed_source_calls: list[str] = []
    original_publish = source_processing._maybe_publish_enriched_shard

    def track_resume_publish(**kwargs):
        resumed_source_calls.append(Path(kwargs["source"]).name)
        return original_publish(**kwargs)

    monkeypatch.setattr(source_processing, "_maybe_publish_enriched_shard", track_resume_publish)
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
    monkeypatch.setattr(source_processing, "_upload_public_shard", lambda *_args: None)
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
        source_processing,
        "_upload_public_shard",
        lambda _run, source, _repo, *_args: uploaded_during_resume.append(source.name),
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
    monkeypatch.setattr(source_processing, "_upload_public_shard", lambda *_args: None)
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
