"""Resumable end-to-end orchestration for a complete source inventory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from osm_polygon_website_tag.application.inventory import (
    discover_sources,
    source_inventory_matches_expected,
)
from osm_polygon_website_tag.application.resume_planner import (
    prepare_resume_priorities,
    prioritize_sources,
)
from osm_polygon_website_tag.application.source_processing import (
    SourcePhaseCounts,
    SourceProcessingContext,
    _run_needs_enrichment,
    _run_needs_language_detection,
    process_sources,
)
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.pipeline.glotlid import LanguageDetector, load_glotlid_detector
from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token
from osm_polygon_website_tag.publishing.incremental import (
    CheckpointV2,
    load_upload_checkpoint,
    reconcile_upload_checkpoint,
)
from osm_polygon_website_tag.publishing.publish import create_repo, publish_to_hf
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.finalize import finalize_run
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.repair import refresh_card_run
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET
from osm_polygon_website_tag.runtime.paths import assert_seagate_path, glotlid_model_cache_dir
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    STATUS_INITIALIZED,
    RunState,
    SourceFingerprint,
    expected_source_inventory,
    initialise_run,
    load_run,
    snapshot_source_fingerprint,
    transition_status,
    upsert_run_metadata,
)
from osm_polygon_website_tag.runtime.safety import assert_path_safe_against, normalize_path


@dataclass(frozen=True)
class WorkflowResult:
    """Summary of one orchestration invocation."""

    run_dir: Path
    source_count: int
    extracted_count: int
    skipped_count: int
    uploaded_count: int
    complete: bool
    dry_run: bool


@dataclass(frozen=True)
class _WorkflowSetup:
    run_dir: Path
    state: RunState
    sources: list[Path]
    fingerprints_by_name: dict[str, SourceFingerprint]
    status: str


def run_all(
    *,
    source_root: Path | str,
    output_root: Path | str,
    run_id: str,
    repo_id: str = DEFAULT_HF_DATASET,
    apply: bool = False,
    ensure_repo: bool = False,
    progress: Callable[[str], None] | None = None,
    area_workers: int | None = None,
    max_in_flight_areas: int | None = None,
    fetch_workers: int | None = None,
    detect_languages: bool = False,
    language_detector: LanguageDetector | None = None,
) -> WorkflowResult:
    """Process each PBF through upload, then analyze and finalize the full run.

    Re-running the same command with the same ``run_id`` resumes from exact
    source, shard, enrichment, and upload checkpoints. Old extracting runs
    reuse every verified bundle. ``KeyboardInterrupt`` is deliberately not
    caught, so Ctrl-C returns control immediately without terminalizing the run.
    A completed run explicitly frozen with ``snapshot_status=done`` and a
    completion receipt is immutable; resuming it returns without source or
    enrichment work.
    Optional worker settings are forwarded to the extraction and enrichment
    stages; ``None`` delegates to their bounded defaults. Language detection
    is opt-in; when enabled, a supplied detector is used for hermetic tests,
    otherwise the pinned GlotLID model is loaded from the Seagate data root.
    """
    source_root_path = normalize_path(source_root)
    output_root_path = assert_path_safe_against(output_root, source_root_path)
    run_dir = output_root_path / run_id
    existing_state = _load_existing_state(run_dir, source_root_path)
    frozen = _frozen_snapshot_result(run_dir, existing_state, apply, progress)
    if frozen is not None:
        return frozen

    setup = _prepare_workflow_setup(
        source_root=source_root_path,
        output_root=output_root_path,
        run_id=run_id,
        run_dir=run_dir,
        existing_state=existing_state,
        progress=progress,
    )
    detector = _prepare_language_detector(
        detect_languages=detect_languages,
        language_detector=language_detector,
        run_dir=setup.run_dir,
    )
    upload_checkpoint = _prepare_upload_checkpoint(
        run_dir=setup.run_dir,
        repo_id=repo_id,
        apply=apply,
        ensure_repo=ensure_repo,
        progress=progress,
    )
    context = SourceProcessingContext(
        run_dir=setup.run_dir,
        state=setup.state,
        repo_id=repo_id,
        apply=apply,
        progress=progress,
        invocation_id=uuid4().hex,
        upload_checkpoint=upload_checkpoint,
        area_workers=area_workers,
        max_in_flight_areas=max_in_flight_areas,
        fetch_workers=fetch_workers,
        detect_languages=detect_languages,
        language_detector=detector,
    )
    processed_names, retry_names = _resume_source_names(
        setup.state,
        upload_checkpoint,
        apply=apply,
    )
    partial_names, retry_priorities = prepare_resume_priorities(
        setup.run_dir,
        setup.state,
        setup.sources,
        retry_names=retry_names,
    )
    ordered_sources = prioritize_sources(
        setup.sources,
        processed_names,
        retry_names=retry_names,
        partial_names=partial_names,
        retry_priorities=retry_priorities,
    )
    status, counts = _run_source_phases(
        setup.status,
        setup.sources,
        ordered_sources,
        setup.fingerprints_by_name,
        context,
    )
    status = _complete_workflow(status, context)
    return WorkflowResult(
        run_dir=setup.run_dir,
        source_count=len(setup.sources),
        extracted_count=counts.extracted,
        skipped_count=counts.reused,
        uploaded_count=counts.uploaded,
        complete=status == STATUS_COMPLETE,
        dry_run=not apply,
    )


def _prepare_language_detector(
    *,
    detect_languages: bool,
    language_detector: LanguageDetector | None,
    run_dir: Path,
) -> LanguageDetector | None:
    """Resolve the opt-in detector without loading model resources by default."""
    if not detect_languages:
        return None
    if language_detector is not None:
        return language_detector
    assert_seagate_path(run_dir, label="run directory")
    model_cache = glotlid_model_cache_dir()
    assert_seagate_path(model_cache, label="GlotLID model cache")
    return load_glotlid_detector(model_cache)


def _load_existing_state(run_dir: Path, source_root: Path) -> RunState | None:
    if not run_dir.exists():
        return None
    state = load_run(run_dir)
    if state.metadata.get("source_root") != str(source_root):
        raise ValueError("existing run source_root does not match this command")
    return state


def _frozen_snapshot_result(
    run_dir: Path,
    state: RunState | None,
    apply: bool,
    progress: Callable[[str], None] | None,
) -> WorkflowResult | None:
    if state is None:
        return None
    if (
        state.metadata.get("status") != STATUS_COMPLETE
        or state.metadata.get("snapshot_status") != "done"
    ):
        return None
    receipt = run_dir / "manifests" / "completion_receipt.json"
    if not receipt.is_file():
        raise ValueError(
            "frozen snapshot is missing its completion receipt; "
            "run finalize-snapshot before resuming"
        )
    _progress(progress, "Frozen snapshot is already complete; skipping enrichment and uploads")
    return WorkflowResult(
        run_dir=run_dir,
        source_count=len(state.sources),
        extracted_count=0,
        skipped_count=0,
        uploaded_count=0,
        complete=True,
        dry_run=not apply,
    )


def _prepare_workflow_setup(
    *,
    source_root: Path,
    output_root: Path,
    run_id: str,
    run_dir: Path,
    existing_state: RunState | None,
    progress: Callable[[str], None] | None,
) -> _WorkflowSetup:
    sources = discover_sources(source_root)
    fingerprints = [snapshot_source_fingerprint(source) for source in sources]
    run_dir, state = _load_or_initialise_state(
        output_root=output_root,
        run_id=run_id,
        run_dir=run_dir,
        fingerprints=fingerprints,
        source_root=source_root,
        existing_state=existing_state,
    )
    status = _validated_status(state.metadata.get("status"))
    state = _reopen_snapshot_if_needed(state)
    state, status = _refresh_legacy_card_if_needed(
        run_dir=run_dir,
        state=state,
        status=status,
        progress=progress,
    )
    return _WorkflowSetup(
        run_dir=run_dir,
        state=state,
        sources=sources,
        fingerprints_by_name={fingerprint.filename: fingerprint for fingerprint in fingerprints},
        status=status,
    )


def _load_or_initialise_state(
    *,
    output_root: Path,
    run_id: str,
    run_dir: Path,
    fingerprints: list[SourceFingerprint],
    source_root: Path,
    existing_state: RunState | None,
) -> tuple[Path, RunState]:
    if existing_state is None:
        run_dir, state = initialise_run(
            output_root,
            run_id=run_id,
            expected_sources=fingerprints,
        )
        upsert_run_metadata(state, {"source_root": str(source_root)})
        return run_dir, state
    expected = expected_source_inventory(run_dir)
    if not source_inventory_matches_expected(expected, fingerprints):
        raise ValueError("source inventory changed since this run was initialized")
    return run_dir, existing_state


def _reopen_snapshot_if_needed(state: RunState) -> RunState:
    if state.metadata.get("snapshot_status") == "done":
        upsert_run_metadata(state, {"snapshot_status": "in_progress"})
    return state


def _refresh_legacy_card_if_needed(
    *,
    run_dir: Path,
    state: RunState,
    status: str,
    progress: Callable[[str], None] | None,
) -> tuple[RunState, str]:
    if status != STATUS_COMPLETE or not _card_refresh_needed(run_dir):
        return state, status
    _progress(progress, "Refreshing the legacy dataset card and H3 density map")
    refreshed = refresh_card_run(run_dir)
    if not refreshed.ok:
        raise ValueError(f"legacy card refresh failed: {refreshed.verification.errors}")
    state = load_run(run_dir)
    return state, _validated_status(state.metadata.get("status"))


def _validated_status(raw_status: object) -> str:
    allowed = {
        STATUS_INITIALIZED,
        STATUS_EXTRACTING,
        STATUS_EXTRACTED,
        STATUS_ENRICHING,
        STATUS_ENRICHED,
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_COMPLETE,
    }
    if raw_status not in allowed:
        raise ValueError(f"run cannot be resumed from terminal status {raw_status!r}")
    return str(raw_status)


def _prepare_upload_checkpoint(
    *,
    run_dir: Path,
    repo_id: str,
    apply: bool,
    ensure_repo: bool,
    progress: Callable[[str], None] | None,
) -> CheckpointV2:
    hf_token = _require_upload_token(apply)
    _ensure_dataset_repo(repo_id, apply=apply, ensure_repo=ensure_repo, progress=progress)
    checkpoint = load_upload_checkpoint(run_dir)
    return _reconcile_checkpoint(
        run_dir=run_dir,
        repo_id=repo_id,
        token=hf_token,
        checkpoint=checkpoint,
        apply=apply,
    )


def _require_upload_token(apply: bool) -> str | None:
    hf_token = resolve_hf_token() if apply else None
    if apply and not hf_token:
        raise ValueError("run-all --apply requires Hugging Face environment/local credentials")
    return hf_token


def _ensure_dataset_repo(
    repo_id: str,
    *,
    apply: bool,
    ensure_repo: bool,
    progress: Callable[[str], None] | None,
) -> None:
    if apply and ensure_repo:
        _progress(progress, f"Ensuring Hugging Face dataset repository {repo_id}")
        create_repo(repo_id=repo_id, exist_ok=True)


def _reconcile_checkpoint(
    *,
    run_dir: Path,
    repo_id: str,
    token: str | None,
    checkpoint: CheckpointV2,
    apply: bool,
) -> CheckpointV2:
    if not apply:
        return checkpoint
    if token is None:
        raise ValueError("apply mode requires Hugging Face credentials")
    return reconcile_upload_checkpoint(
        run_dir,
        repo_id=repo_id,
        token=token,
    )


def _resume_source_names(
    state: RunState,
    checkpoint: CheckpointV2,
    *,
    apply: bool,
) -> tuple[set[str], set[str]]:
    if apply:
        return _applied_resume_names(state, checkpoint)
    return _dry_run_resume_names(state)


def _applied_resume_names(
    state: RunState,
    checkpoint: CheckpointV2,
) -> tuple[set[str], set[str]]:
    acknowledged_names = set(checkpoint["sources"])
    processed_names = _acknowledged_processed_names(state, acknowledged_names)
    retry_names = _acknowledged_retry_names(state, acknowledged_names, processed_names)
    return processed_names, retry_names


def _acknowledged_processed_names(
    state: RunState,
    acknowledged_names: set[str],
) -> set[str]:
    return {
        name
        for name, entry in state.sources.items()
        if name in acknowledged_names and entry.get("enrichment_pending") is False
    }


def _acknowledged_retry_names(
    state: RunState,
    acknowledged_names: set[str],
    processed_names: set[str],
) -> set[str]:
    return {
        name for name in acknowledged_names if name in state.sources and name not in processed_names
    }


def _dry_run_resume_names(state: RunState) -> tuple[set[str], set[str]]:
    processed_names = {
        name for name, entry in state.sources.items() if entry.get("enrichment_pending") is False
    }
    return processed_names, set(state.sources) - processed_names


def _run_source_phases(
    status: str,
    sources: list[Path],
    ordered_sources: list[Path],
    fingerprints_by_name: dict[str, SourceFingerprint],
    context: SourceProcessingContext,
) -> tuple[str, SourcePhaseCounts]:
    if status in {STATUS_INITIALIZED, STATUS_EXTRACTING}:
        status, counts = _run_extraction_phase(
            status,
            sources,
            ordered_sources,
            fingerprints_by_name,
            context,
        )
    else:
        counts = SourcePhaseCounts()
    status = _enter_enrichment_phase_if_needed(status, context)
    if status != STATUS_ENRICHING:
        return status, counts
    status, enrichment_counts = _run_enrichment_phase(
        sources,
        ordered_sources,
        fingerprints_by_name,
        context,
    )
    return status, _add_phase_counts(counts, enrichment_counts)


def _enter_enrichment_phase_if_needed(status: str, context: SourceProcessingContext) -> str:
    if status == STATUS_EXTRACTED:
        return _transition_to_enriching(context)
    if status not in {STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}:
        return status
    if not _run_requires_enrichment(context):
        return status
    return _transition_to_enriching(context)


def _run_requires_enrichment(context: SourceProcessingContext) -> bool:
    return _run_needs_enrichment(context.run_dir) or (
        context.detect_languages and _run_needs_language_detection(context.run_dir)
    )


def _transition_to_enriching(context: SourceProcessingContext) -> str:
    transition_status(context.state, STATUS_ENRICHING)
    return STATUS_ENRICHING


def _run_extraction_phase(
    status: str,
    sources: list[Path],
    ordered_sources: list[Path],
    fingerprints_by_name: dict[str, SourceFingerprint],
    context: SourceProcessingContext,
) -> tuple[str, SourcePhaseCounts]:
    if status == STATUS_INITIALIZED:
        transition_status(context.state, STATUS_EXTRACTING)
    counts = process_sources(
        sources=sources,
        ordered_sources=ordered_sources,
        fingerprints_by_name=fingerprints_by_name,
        context=context,
        allow_extraction=True,
    )
    transition_status(context.state, STATUS_EXTRACTED)
    transition_status(context.state, STATUS_ENRICHING)
    transition_status(context.state, STATUS_ENRICHED)
    return STATUS_ENRICHED, counts


def _run_enrichment_phase(
    sources: list[Path],
    ordered_sources: list[Path],
    fingerprints_by_name: dict[str, SourceFingerprint],
    context: SourceProcessingContext,
) -> tuple[str, SourcePhaseCounts]:
    counts = process_sources(
        sources=sources,
        ordered_sources=ordered_sources,
        fingerprints_by_name=fingerprints_by_name,
        context=context,
        allow_extraction=False,
    )
    transition_status(context.state, STATUS_ENRICHED)
    return STATUS_ENRICHED, counts


def _add_phase_counts(left: SourcePhaseCounts, right: SourcePhaseCounts) -> SourcePhaseCounts:
    return SourcePhaseCounts(
        extracted=left.extracted + right.extracted,
        reused=left.reused + right.reused,
        uploaded=left.uploaded + right.uploaded,
    )


def _complete_workflow(status: str, context: SourceProcessingContext) -> str:
    status = _build_analysis_if_needed(status, context)
    status = _build_card_if_needed(status, context)
    status = _finalize_if_needed(status, context)
    _publish_complete_run(status, context)
    return status


def _build_analysis_if_needed(status: str, context: SourceProcessingContext) -> str:
    if status != STATUS_ENRICHED:
        return status
    _progress(context.progress, "Building aggregate analysis")
    analyze_results(context.run_dir)
    transition_status(context.state, STATUS_ANALYZED)
    return STATUS_ANALYZED


def _build_card_if_needed(status: str, context: SourceProcessingContext) -> str:
    if status != STATUS_ANALYZED:
        return status
    _progress(context.progress, "Building artifact-derived dataset card")
    build_card(context.run_dir)
    transition_status(context.state, STATUS_CARD_BUILT)
    return STATUS_CARD_BUILT


def _finalize_if_needed(status: str, context: SourceProcessingContext) -> str:
    if status != STATUS_CARD_BUILT:
        return status
    _progress(context.progress, "Verifying and finalizing the complete run")
    final = finalize_run(context.run_dir)
    if not final.ok:
        raise ValueError(f"final verification failed: {final.verification.errors}")
    return STATUS_COMPLETE


def _publish_complete_run(status: str, context: SourceProcessingContext) -> None:
    if status == STATUS_COMPLETE and context.apply:
        _progress(context.progress, "Uploading the receipt-bound complete dataset")
        publish_to_hf(context.run_dir, repo_id=context.repo_id, dry_run=False)


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _card_refresh_needed(run_dir: Path) -> bool:
    """Return whether a completed run lacks the current card contract."""
    if not (run_dir / POLYGON_DENSITY_ASSET_REL_PATH).is_file():
        return True
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return not isinstance(receipt, dict) or receipt.get("card_contract_version") != 1


__all__ = ["WorkflowResult", "discover_sources", "prioritize_sources", "run_all"]
