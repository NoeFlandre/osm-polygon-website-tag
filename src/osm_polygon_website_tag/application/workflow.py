"""Resumable end-to-end orchestration for a complete source inventory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast
from uuid import uuid4

import pyarrow.parquet as pq

from osm_polygon_website_tag.application.inventory import (
    discover_sources,
    source_bundle_is_complete,
    source_inventory_matches_expected,
)
from osm_polygon_website_tag.application.resume_planner import (
    coerce_enrichment_status_summary,
    prepare_resume_priorities,
    prioritize_sources,
    summarize_enrichment_status,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    schema_matches,
)
from osm_polygon_website_tag.contracts.text_schema import status_has_retryable_value
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.pipeline.enrich import EnrichmentResult, enrich_polygon_shard
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.pipeline.public_schema_migration import migrate_public_shard
from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token
from osm_polygon_website_tag.publishing.incremental import (
    CheckpointV2,
    IncrementalPublishPlan,
    incremental_publish_changed_shard,
    load_upload_checkpoint,
    persist_successful_upload,
    reconcile_upload_checkpoint,
)
from osm_polygon_website_tag.publishing.publish import _upload_folder, create_repo, publish_to_hf
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.finalize import finalize_run
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.repair import refresh_card_run
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET
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
    update_public_shard_metadata,
    update_source_enrichment_status,
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
class _SourceTransactionResult:
    extracted: bool
    reused: bool
    uploaded: bool


@dataclass(frozen=True)
class _WorkflowSetup:
    run_dir: Path
    state: RunState
    sources: list[Path]
    fingerprints_by_name: dict[str, SourceFingerprint]
    status: str


@dataclass(frozen=True)
class _SourceRunContext:
    run_dir: Path
    state: RunState
    repo_id: str
    apply: bool
    progress: Callable[[str], None] | None
    invocation_id: str
    upload_checkpoint: CheckpointV2
    area_workers: int | None
    max_in_flight_areas: int | None
    fetch_workers: int | None


@dataclass(frozen=True)
class _PhaseCounts:
    extracted: int = 0
    reused: int = 0
    uploaded: int = 0


@dataclass(frozen=True)
class _SourceBundleResult:
    shard: Path
    extracted: bool
    reused: bool


@dataclass(frozen=True)
class _EnrichmentDecision:
    needs_enrichment: bool
    status_summary: dict[str, dict[str, int]] | None


class _ExtractionKwargs(TypedDict, total=False):
    run_state: RunState
    area_workers: int
    max_in_flight_areas: int


class _EnrichmentKwargs(TypedDict, total=False):
    cache_path: Path
    invocation_id: str
    fetch_workers: int | None


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
    stages; ``None`` delegates to their bounded defaults.
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
    upload_checkpoint = _prepare_upload_checkpoint(
        run_dir=setup.run_dir,
        repo_id=repo_id,
        apply=apply,
        ensure_repo=ensure_repo,
        progress=progress,
    )
    context = _SourceRunContext(
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
    return reconcile_upload_checkpoint(
        run_dir,
        repo_id=repo_id,
        token=cast(str, token),
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
    context: _SourceRunContext,
) -> tuple[str, _PhaseCounts]:
    if status in {STATUS_INITIALIZED, STATUS_EXTRACTING}:
        status, counts = _run_extraction_phase(
            status,
            sources,
            ordered_sources,
            fingerprints_by_name,
            context,
        )
    else:
        counts = _PhaseCounts()
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


def _enter_enrichment_phase_if_needed(status: str, context: _SourceRunContext) -> str:
    if status == STATUS_EXTRACTED or (
        status in {STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}
        and _run_needs_enrichment(context.run_dir)
    ):
        transition_status(context.state, STATUS_ENRICHING)
        return STATUS_ENRICHING
    return status


def _run_extraction_phase(
    status: str,
    sources: list[Path],
    ordered_sources: list[Path],
    fingerprints_by_name: dict[str, SourceFingerprint],
    context: _SourceRunContext,
) -> tuple[str, _PhaseCounts]:
    if status == STATUS_INITIALIZED:
        transition_status(context.state, STATUS_EXTRACTING)
    counts = _process_source_batch(
        sources,
        ordered_sources,
        fingerprints_by_name,
        context,
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
    context: _SourceRunContext,
) -> tuple[str, _PhaseCounts]:
    counts = _process_source_batch(
        sources,
        ordered_sources,
        fingerprints_by_name,
        context,
        allow_extraction=False,
    )
    transition_status(context.state, STATUS_ENRICHED)
    return STATUS_ENRICHED, counts


def _process_source_batch(
    sources: list[Path],
    ordered_sources: list[Path],
    fingerprints_by_name: dict[str, SourceFingerprint],
    context: _SourceRunContext,
    *,
    allow_extraction: bool,
) -> _PhaseCounts:
    counts = _PhaseCounts()
    for index, source in enumerate(ordered_sources, start=1):
        result = _process_source(
            source=source,
            fingerprint=fingerprints_by_name[source.name],
            context=context,
            index=index,
            total=len(sources),
            allow_extraction=allow_extraction,
        )
        counts = _add_phase_counts(
            counts,
            _PhaseCounts(
                extracted=int(result.extracted),
                reused=int(result.reused),
                uploaded=int(result.uploaded),
            ),
        )
    return counts


def _add_phase_counts(left: _PhaseCounts, right: _PhaseCounts) -> _PhaseCounts:
    return _PhaseCounts(
        extracted=left.extracted + right.extracted,
        reused=left.reused + right.reused,
        uploaded=left.uploaded + right.uploaded,
    )


def _complete_workflow(status: str, context: _SourceRunContext) -> str:
    status = _build_analysis_if_needed(status, context)
    status = _build_card_if_needed(status, context)
    status = _finalize_if_needed(status, context)
    _publish_complete_run(status, context)
    return status


def _build_analysis_if_needed(status: str, context: _SourceRunContext) -> str:
    if status != STATUS_ENRICHED:
        return status
    _progress(context.progress, "Building aggregate analysis")
    analyze_results(context.run_dir)
    transition_status(context.state, STATUS_ANALYZED)
    return STATUS_ANALYZED


def _build_card_if_needed(status: str, context: _SourceRunContext) -> str:
    if status != STATUS_ANALYZED:
        return status
    _progress(context.progress, "Building artifact-derived dataset card")
    build_card(context.run_dir)
    transition_status(context.state, STATUS_CARD_BUILT)
    return STATUS_CARD_BUILT


def _finalize_if_needed(status: str, context: _SourceRunContext) -> str:
    if status != STATUS_CARD_BUILT:
        return status
    _progress(context.progress, "Verifying and finalizing the complete run")
    final = finalize_run(context.run_dir)
    if not final.ok:
        raise ValueError(f"final verification failed: {final.verification.errors}")
    return STATUS_COMPLETE


def _publish_complete_run(status: str, context: _SourceRunContext) -> None:
    if status == STATUS_COMPLETE and context.apply:
        _progress(context.progress, "Uploading the receipt-bound complete dataset")
        publish_to_hf(context.run_dir, repo_id=context.repo_id, dry_run=False)


def _process_source(
    *,
    source: Path,
    fingerprint: SourceFingerprint,
    context: _SourceRunContext,
    index: int,
    total: int,
    allow_extraction: bool,
) -> _SourceTransactionResult:
    bundle = _ensure_source_bundle(
        source=source,
        fingerprint=fingerprint,
        context=context,
        index=index,
        total=total,
        allow_extraction=allow_extraction,
    )
    migration_changed = _migrate_public_shard_if_needed(
        source=source,
        shard=bundle.shard,
        context=context,
        index=index,
        total=total,
    )
    decision = _enrich_source_shard_if_needed(
        source=source,
        shard=bundle.shard,
        context=context,
        index=index,
        total=total,
        migration_changed=migration_changed,
    )
    uploaded = _publish_source_if_needed(
        source=source,
        context=context,
        index=index,
        total=total,
        reused=bundle.reused,
        migration_changed=migration_changed,
        needs_enrichment=decision.needs_enrichment,
    )
    return _SourceTransactionResult(
        extracted=bundle.extracted,
        reused=bundle.reused,
        uploaded=uploaded,
    )


def _ensure_source_bundle(
    *,
    source: Path,
    fingerprint: SourceFingerprint,
    context: _SourceRunContext,
    index: int,
    total: int,
    allow_extraction: bool,
) -> _SourceBundleResult:
    if source_bundle_is_complete(
        context.run_dir,
        context.state.sources.get(source.name),
        fingerprint,
    ):
        if allow_extraction:
            _progress(context.progress, f"[{index}/{total}] Resuming: {source.name} is complete")
        return _SourceBundleResult(
            shard=_public_shard_path(context.run_dir, source),
            extracted=False,
            reused=allow_extraction,
        )
    if not allow_extraction:
        raise ValueError(f"cannot enrich incomplete source bundle: {source.name}")
    _progress(context.progress, f"[{index}/{total}] Extracting {source.name}")
    _extract_with_options(source, context)
    if not source_bundle_is_complete(
        context.run_dir,
        context.state.sources.get(source.name),
        fingerprint,
    ):
        raise ValueError(f"source bundle is incomplete after extraction: {source.name}")
    return _SourceBundleResult(
        shard=_public_shard_path(context.run_dir, source),
        extracted=True,
        reused=False,
    )


def _extract_with_options(source: Path, context: _SourceRunContext) -> None:
    kwargs: _ExtractionKwargs = {"run_state": context.state}
    if context.area_workers is not None:
        kwargs["area_workers"] = context.area_workers
    if context.max_in_flight_areas is not None:
        kwargs["max_in_flight_areas"] = context.max_in_flight_areas
    extract_pbf(source, context.run_dir, **kwargs)


def _migrate_public_shard_if_needed(
    source: Path,
    shard: Path,
    context: _SourceRunContext,
    index: int,
    total: int,
) -> bool:
    if not schema_matches(pq.read_schema(shard), POLYGON_PUBLIC_SCHEMA_V1_2):
        return False
    migration = migrate_public_shard(shard)
    _progress(context.progress, f"[{index}/{total}] Migrating {source.name} to public schema v1.3")
    update_public_shard_metadata(
        context.state,
        filename=source.name,
        row_count=migration.row_count,
        shard_sha256=migration.shard_sha256,
    )
    return migration.changed


def _enrich_source_shard_if_needed(
    *,
    source: Path,
    shard: Path,
    context: _SourceRunContext,
    index: int,
    total: int,
    migration_changed: bool,
) -> _EnrichmentDecision:
    manifest_entry = context.state.sources[source.name]
    marker = manifest_entry.get("enrichment_pending")
    status_summary = coerce_enrichment_status_summary(
        manifest_entry.get("enrichment_status_counts")
    )
    decision = _initial_enrichment_decision(
        shard,
        marker=marker,
        status_summary=status_summary,
        migration_changed=migration_changed,
    )
    if decision.needs_enrichment:
        _progress(context.progress, f"[{index}/{total}] Enriching {source.name}")
        enrichment = _enrich_shard(shard, context)
        update_public_shard_metadata(
            context.state,
            filename=source.name,
            row_count=enrichment.row_count,
            shard_sha256=enrichment.shard_sha256,
        )
        decision = _EnrichmentDecision(
            needs_enrichment=_shard_needs_enrichment(shard),
            status_summary=summarize_enrichment_status(shard),
        )
    elif decision.status_summary is None:
        decision = _EnrichmentDecision(
            needs_enrichment=False,
            status_summary=summarize_enrichment_status(shard),
        )
        _progress(context.progress, f"[{index}/{total}] Resuming: {source.name} text is complete")
    else:
        _progress(context.progress, f"[{index}/{total}] Resuming: {source.name} text is complete")
    update_source_enrichment_status(
        context.state,
        filename=source.name,
        pending=decision.needs_enrichment,
        status_counts=decision.status_summary,
    )
    return decision


def _initial_enrichment_decision(
    shard: Path,
    *,
    marker: object,
    status_summary: dict[str, dict[str, int]] | None,
    migration_changed: bool,
) -> _EnrichmentDecision:
    if _should_recheck_enrichment(
        marker=marker,
        status_summary=status_summary,
        migration_changed=migration_changed,
    ):
        needs_enrichment = _shard_needs_enrichment(shard)
    else:
        assert isinstance(marker, bool)
        needs_enrichment = marker
    return _EnrichmentDecision(
        needs_enrichment=needs_enrichment,
        status_summary=status_summary,
    )


def _should_recheck_enrichment(
    *,
    marker: object,
    status_summary: dict[str, dict[str, int]] | None,
    migration_changed: bool,
) -> bool:
    return (
        migration_changed
        or not isinstance(marker, bool)
        or (marker is False and status_summary is None)
    )


def _enrich_shard(shard: Path, context: _SourceRunContext) -> EnrichmentResult:
    kwargs: _EnrichmentKwargs = {
        "cache_path": context.run_dir / "cache" / "website_text.sqlite3",
        "invocation_id": context.invocation_id,
    }
    if context.fetch_workers is not None:
        kwargs["fetch_workers"] = context.fetch_workers
    return enrich_polygon_shard(shard, **kwargs)


def _publish_source_if_needed(
    *,
    source: Path,
    context: _SourceRunContext,
    index: int,
    total: int,
    reused: bool,
    migration_changed: bool,
    needs_enrichment: bool,
) -> bool:
    if _source_upload_is_current_for_context(
        source=source,
        context=context,
        index=index,
        total=total,
        migration_changed=migration_changed,
        needs_enrichment=needs_enrichment,
    ):
        return False
    if not _source_requires_publication(
        context=context,
        migration_changed=migration_changed,
        needs_enrichment=needs_enrichment,
    ):
        return False
    uploaded = _maybe_publish_enriched_shard(
        run_dir=context.run_dir,
        source=source,
        repo_id=context.repo_id,
        apply=context.apply,
        progress=context.progress,
        index=index,
        total=total,
        allow_bundle_only=not reused,
        published_source_names=_published_source_names(context, source),
    )
    _record_source_upload(source, context, uploaded)
    return uploaded


def _source_upload_is_current_for_context(
    *,
    source: Path,
    context: _SourceRunContext,
    index: int,
    total: int,
    migration_changed: bool,
    needs_enrichment: bool,
) -> bool:
    if (
        not context.apply
        or migration_changed
        or needs_enrichment
        or not _source_upload_is_current(
            context.state.sources[source.name],
            source.name,
            context.upload_checkpoint,
        )
    ):
        return False
    _progress(context.progress, f"[{index}/{total}] Resuming: {source.name} is already uploaded")
    return True


def _source_requires_publication(
    *,
    context: _SourceRunContext,
    migration_changed: bool,
    needs_enrichment: bool,
) -> bool:
    return migration_changed or needs_enrichment or context.apply


def _record_source_upload(
    source: Path,
    context: _SourceRunContext,
    uploaded: bool,
) -> None:
    if not uploaded:
        return
    manifest_entry = context.state.sources[source.name]
    context.upload_checkpoint["sources"].__setitem__(
        source.name,
        {"polygon_sha256": str(manifest_entry["public_shard_sha256"])},
    )


def _published_source_names(
    context: _SourceRunContext,
    source: Path,
) -> set[str] | None:
    if not context.apply:
        return None
    names = set(context.upload_checkpoint["sources"])
    names.add(source.name)
    return names


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _source_upload_is_current(
    manifest_entry: dict[str, object],
    filename: str,
    checkpoint: CheckpointV2,
) -> bool:
    sources = checkpoint.get("sources")
    if not isinstance(sources, dict):
        return False
    uploaded = sources.get(filename)
    return isinstance(uploaded, dict) and uploaded.get("polygon_sha256") == manifest_entry.get(
        "public_shard_sha256"
    )


def _public_shard_path(run_dir: Path, source: Path) -> Path:
    return run_dir / "polygons" / f"{source.name.removesuffix('.osm.pbf')}.parquet"


def _upload_public_shard(
    run_dir: Path,
    source: Path,
    repo_id: str,
    plan: IncrementalPublishPlan | None = None,
) -> None:
    if plan is not None and plan.source_filename != source.name:
        raise ValueError("incremental publish plan does not match source")
    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    if plan is not None:
        _upload_folder(
            run_dir,
            repo_id=repo_id,
            repo_kind="dataset",
            artifact_paths=plan.upload_paths,
        )
        return
    if not map_path.is_file():
        shard = _public_shard_path(run_dir, source)
        _upload_folder(
            run_dir,
            repo_id=repo_id,
            repo_kind="dataset",
            artifact_paths=[shard, run_dir / "README.md", run_dir / "dataset.yaml"],
        )
        return
    incremental_publish_changed_shard(
        run_dir,
        source,
        repo_id=repo_id,
        repo_kind="dataset",
        dry_run=False,
        uploader=_upload_folder,
    )


def _maybe_publish_enriched_shard(
    *,
    run_dir: Path,
    source: Path,
    repo_id: str,
    apply: bool,
    progress: Callable[[str], None] | None,
    index: int,
    total: int,
    allow_bundle_only: bool = True,
    published_source_names: set[str] | None = None,
) -> bool:
    """Build the card, compute the checkpoint, and conditionally upload.

    Owns the "build card -> checkpoint -> upload -> persist checkpoint"
    transaction for one enriched shard. Always rebuilds the card so that
    README.md/dataset.yaml reflect the current shard state. Returns
    ``True`` iff a new incremental upload was performed; the caller
    updates ``uploaded_count`` from this return value.
    """
    build_card(run_dir, source_names=published_source_names)
    preview = incremental_publish_changed_shard(run_dir, source, dry_run=True)
    if not apply:
        return False
    if not preview.shard_changed and not allow_bundle_only:
        return False
    if not preview.upload_paths:
        return False
    _progress(
        progress,
        f"[{index}/{total}] Uploading enriched shard and recomputed card",
    )
    _upload_public_shard(run_dir, source, repo_id, preview)
    persist_successful_upload(
        run_dir,
        source,
        shard_sha256=preview.shard_sha256,
        bundle_state=preview.bundle_state,
    )
    return bool(preview.upload_paths)


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


def _run_needs_enrichment(run_dir: Path) -> bool:
    for shard in sorted((run_dir / "polygons").glob("*.parquet")):
        if _shard_needs_enrichment(shard):
            return True
    return False


def _shard_needs_enrichment(shard: Path) -> bool:
    parquet = pq.ParquetFile(shard)
    if _schema_needs_enrichment(parquet):
        return True
    return _status_columns_need_enrichment(parquet)


def _schema_needs_enrichment(parquet: pq.ParquetFile) -> bool:
    schema = parquet.schema_arrow
    return schema_matches(schema, POLYGON_PUBLIC_SCHEMA_V1_1) or not schema_matches(
        schema, POLYGON_PUBLIC_SCHEMA
    )


def _status_columns_need_enrichment(parquet: pq.ParquetFile) -> bool:
    for batch in parquet.iter_batches(
        columns=["website_text_status", "contact_website_text_status"],
        batch_size=8_192,
    ):
        if status_has_retryable_value(batch.column("website_text_status")):
            return True
        if status_has_retryable_value(batch.column("contact_website_text_status")):
            return True
    return False


__all__ = ["WorkflowResult", "discover_sources", "prioritize_sources", "run_all"]
