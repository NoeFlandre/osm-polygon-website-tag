"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
from tests.application.workflow_helpers import (
    RecordingLanguageDetector,
)
from tests.application.workflow_helpers import (
    checkpoint as _checkpoint,
)
from tests.application.workflow_helpers import (
    noop_progress as _noop_progress,
)
from tests.application.workflow_helpers import (
    write_card_contract_fixture as _write_card_contract_fixture,
)

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
)
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_COMPLETE,
    STATUS_ENRICHING,
    STATUS_INITIALIZED,
    RunState,
    SourceFingerprint,
)
from osm_polygon_website_tag.web.politeness import HostPolicy

pytestmark = pytest.mark.usefixtures("offline_remote_reconciliation", "static_text_enrichment")


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


@pytest.mark.parametrize(
    "receipt",
    [
        {"card_contract_version": 1},
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
    _write_card_contract_fixture(tmp_path, {"card_contract_version": 2})
    assert not workflow._card_refresh_needed(tmp_path)


def test_card_refresh_needed_requires_the_map_and_readable_receipt(tmp_path: Path) -> None:
    assert workflow._card_refresh_needed(tmp_path)
    _write_card_contract_fixture(tmp_path, {"card_contract_version": 2})
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
            return self.parts in {
                ("assets/geographic_polygon_density.png",),
                ("stats.json",),
            }

        def read_text(self, *, encoding: str) -> str:
            assert self.parts == ("manifests", "completion_receipt.json")
            assert encoding == "utf-8"
            return '{"card_contract_version": 2}'

    assert not workflow._card_refresh_needed(cast(Any, PathSpy()))


def test_run_all_forwards_each_orchestration_boundary_exactly(  # noqa: PLR0915 - too long or branchy; TODO(#76) split with the fixture work
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
        host_policy=HostPolicy(concurrency=3, delay_seconds=0.5),
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
    assert context.host_policy == HostPolicy(concurrency=3, delay_seconds=0.5)
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
