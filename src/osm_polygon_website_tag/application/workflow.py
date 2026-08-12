"""Resumable end-to-end orchestration for a complete source inventory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
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
)
from osm_polygon_website_tag.contracts.text_schema import status_has_retryable_value
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.pipeline.enrich import enrich_polygon_shard
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.pipeline.public_schema_migration import migrate_public_shard
from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token
from osm_polygon_website_tag.publishing.incremental import (
    CheckpointV2,
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
    Optional worker settings are forwarded to the extraction and enrichment
    stages; ``None`` delegates to their bounded defaults.
    """
    source_root_path = normalize_path(source_root)
    output_root_path = assert_path_safe_against(output_root, source_root_path)
    sources = discover_sources(source_root_path)
    fingerprints = [snapshot_source_fingerprint(source) for source in sources]
    run_dir = output_root_path / run_id

    if run_dir.exists():
        state = load_run(run_dir)
        if state.metadata.get("source_root") != str(source_root_path):
            raise ValueError("existing run source_root does not match this command")
        expected = expected_source_inventory(run_dir)
        if not source_inventory_matches_expected(expected, fingerprints):
            raise ValueError("source inventory changed since this run was initialized")
    else:
        run_dir, state = initialise_run(
            output_root_path,
            run_id=run_id,
            expected_sources=fingerprints,
        )
        upsert_run_metadata(state, {"source_root": str(source_root_path)})

    status = state.metadata.get("status")
    if status not in {
        STATUS_INITIALIZED,
        STATUS_EXTRACTING,
        STATUS_EXTRACTED,
        STATUS_ENRICHING,
        STATUS_ENRICHED,
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_COMPLETE,
    }:
        raise ValueError(f"run cannot be resumed from terminal status {status!r}")

    if status == STATUS_COMPLETE and _card_refresh_needed(run_dir):
        _progress(progress, "Refreshing the legacy dataset card and H3 density map")
        refreshed = refresh_card_run(run_dir)
        if not refreshed.ok:
            raise ValueError(f"legacy card refresh failed: {refreshed.verification.errors}")
        state = load_run(run_dir)
        status = state.metadata.get("status")

    hf_token = resolve_hf_token() if apply else None
    if apply and not hf_token:
        raise ValueError("run-all --apply requires Hugging Face environment/local credentials")
    if apply and ensure_repo:
        _progress(progress, f"Ensuring Hugging Face dataset repository {repo_id}")
        create_repo(repo_id=repo_id, exist_ok=True)

    extracted_count = 0
    skipped_count = 0
    uploaded_count = 0
    invocation_id = uuid4().hex
    fingerprints_by_name = {fingerprint.filename: fingerprint for fingerprint in fingerprints}
    upload_checkpoint = load_upload_checkpoint(run_dir)
    if apply:
        upload_checkpoint = reconcile_upload_checkpoint(
            run_dir,
            repo_id=repo_id,
            token=cast(str, hf_token),
        )
    if apply:
        acknowledged_names = set(upload_checkpoint["sources"])
        processed_names = {
            name
            for name, entry in state.sources.items()
            if name in acknowledged_names and entry.get("enrichment_pending") is False
        }
        retry_names = {
            name
            for name in acknowledged_names
            if name in state.sources and name not in processed_names
        }
    else:
        processed_names = {
            name
            for name, entry in state.sources.items()
            if entry.get("enrichment_pending") is False
        }
        retry_names = set(state.sources) - processed_names
    partial_names, retry_priorities = prepare_resume_priorities(
        run_dir,
        state,
        sources,
        retry_names=retry_names,
    )
    ordered_sources = prioritize_sources(
        sources,
        processed_names,
        retry_names=retry_names,
        partial_names=partial_names,
        retry_priorities=retry_priorities,
    )
    if status in {STATUS_INITIALIZED, STATUS_EXTRACTING}:
        if status == STATUS_INITIALIZED:
            transition_status(state, STATUS_EXTRACTING)
        for index, (source, fingerprint) in enumerate(
            ((source, fingerprints_by_name[source.name]) for source in ordered_sources),
            start=1,
        ):
            result = _process_source(
                source=source,
                fingerprint=fingerprint,
                run_dir=run_dir,
                state=state,
                repo_id=repo_id,
                apply=apply,
                progress=progress,
                index=index,
                total=len(sources),
                invocation_id=invocation_id,
                allow_extraction=True,
                upload_checkpoint=upload_checkpoint,
                area_workers=area_workers,
                max_in_flight_areas=max_in_flight_areas,
                fetch_workers=fetch_workers,
            )
            extracted_count += int(result.extracted)
            skipped_count += int(result.reused)
            uploaded_count += int(result.uploaded)
        transition_status(state, STATUS_EXTRACTED)
        status = STATUS_EXTRACTED
        transition_status(state, STATUS_ENRICHING)
        status = STATUS_ENRICHING
        transition_status(state, STATUS_ENRICHED)
        status = STATUS_ENRICHED

    migration_statuses = {
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_COMPLETE,
    }
    if status == STATUS_EXTRACTED or (
        status in migration_statuses and _run_needs_enrichment(run_dir)
    ):
        transition_status(state, STATUS_ENRICHING)
        status = STATUS_ENRICHING

    if status == STATUS_ENRICHING:
        for index, (source, fingerprint) in enumerate(
            ((source, fingerprints_by_name[source.name]) for source in ordered_sources),
            start=1,
        ):
            result = _process_source(
                source=source,
                fingerprint=fingerprint,
                run_dir=run_dir,
                state=state,
                repo_id=repo_id,
                apply=apply,
                progress=progress,
                index=index,
                total=len(sources),
                invocation_id=invocation_id,
                allow_extraction=False,
                upload_checkpoint=upload_checkpoint,
                area_workers=area_workers,
                max_in_flight_areas=max_in_flight_areas,
                fetch_workers=fetch_workers,
            )
            uploaded_count += int(result.uploaded)
        transition_status(state, STATUS_ENRICHED)
        status = STATUS_ENRICHED

    if status == STATUS_ENRICHED:
        _progress(progress, "Building aggregate analysis")
        analyze_results(run_dir)
        transition_status(state, STATUS_ANALYZED)
        status = STATUS_ANALYZED
    if status == STATUS_ANALYZED:
        _progress(progress, "Building artifact-derived dataset card")
        build_card(run_dir)
        transition_status(state, STATUS_CARD_BUILT)
        status = STATUS_CARD_BUILT
    if status == STATUS_CARD_BUILT:
        _progress(progress, "Verifying and finalizing the complete run")
        final = finalize_run(run_dir)
        if not final.ok:
            raise ValueError(f"final verification failed: {final.verification.errors}")
        status = STATUS_COMPLETE
    if status == STATUS_COMPLETE and apply:
        _progress(progress, "Uploading the receipt-bound complete dataset")
        publish_to_hf(run_dir, repo_id=repo_id, dry_run=False)

    return WorkflowResult(
        run_dir=run_dir,
        source_count=len(sources),
        extracted_count=extracted_count,
        skipped_count=skipped_count,
        uploaded_count=uploaded_count,
        complete=status == STATUS_COMPLETE,
        dry_run=not apply,
    )


def _process_source(
    *,
    source: Path,
    fingerprint: SourceFingerprint,
    run_dir: Path,
    state: RunState,
    repo_id: str,
    apply: bool,
    progress: Callable[[str], None] | None,
    index: int,
    total: int,
    invocation_id: str,
    allow_extraction: bool,
    upload_checkpoint: CheckpointV2,
    area_workers: int | None,
    max_in_flight_areas: int | None,
    fetch_workers: int | None,
) -> _SourceTransactionResult:
    bundle_complete = source_bundle_is_complete(
        run_dir,
        state.sources.get(source.name),
        fingerprint,
    )
    extracted = False
    reused = False
    if not bundle_complete:
        if not allow_extraction:
            raise ValueError(f"cannot enrich incomplete source bundle: {source.name}")
        _progress(progress, f"[{index}/{total}] Extracting {source.name}")
        if area_workers is None and max_in_flight_areas is None:
            extract_pbf(source, run_dir, run_state=state)
        elif area_workers is None and max_in_flight_areas is not None:
            extract_pbf(
                source,
                run_dir,
                run_state=state,
                max_in_flight_areas=max_in_flight_areas,
            )
        elif area_workers is not None and max_in_flight_areas is None:
            extract_pbf(source, run_dir, run_state=state, area_workers=area_workers)
        else:
            assert area_workers is not None
            assert max_in_flight_areas is not None
            extract_pbf(
                source,
                run_dir,
                run_state=state,
                area_workers=area_workers,
                max_in_flight_areas=max_in_flight_areas,
            )
        extracted = True
        if not source_bundle_is_complete(
            run_dir,
            state.sources.get(source.name),
            fingerprint,
        ):
            raise ValueError(f"source bundle is incomplete after extraction: {source.name}")
    elif allow_extraction:
        reused = True
        _progress(progress, f"[{index}/{total}] Resuming: {source.name} is complete")

    shard = _public_shard_path(run_dir, source)
    manifest_entry = state.sources[source.name]
    migration_changed = False
    if pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA_V1_2, check_metadata=True):
        migration = migrate_public_shard(shard)
        migration_changed = migration.changed
        _progress(progress, f"[{index}/{total}] Migrating {source.name} to public schema v1.3")
        update_public_shard_metadata(
            state,
            filename=source.name,
            row_count=migration.row_count,
            shard_sha256=migration.shard_sha256,
        )
    marker = manifest_entry.get("enrichment_pending")
    status_summary = coerce_enrichment_status_summary(
        manifest_entry.get("enrichment_status_counts")
    )
    needs_enrichment = (
        _shard_needs_enrichment(shard)
        if (
            migration_changed
            or not isinstance(marker, bool)
            or (marker is False and status_summary is None)
        )
        else marker
    )
    if needs_enrichment:
        _progress(progress, f"[{index}/{total}] Enriching {source.name}")
        if fetch_workers is None:
            enrichment = enrich_polygon_shard(
                shard,
                cache_path=run_dir / "cache" / "website_text.sqlite3",
                invocation_id=invocation_id,
            )
        else:
            enrichment = enrich_polygon_shard(
                shard,
                cache_path=run_dir / "cache" / "website_text.sqlite3",
                invocation_id=invocation_id,
                fetch_workers=fetch_workers,
            )
        update_public_shard_metadata(
            state,
            filename=source.name,
            row_count=enrichment.row_count,
            shard_sha256=enrichment.shard_sha256,
        )
        needs_enrichment = _shard_needs_enrichment(shard)
        status_summary = summarize_enrichment_status(shard)
    else:
        if status_summary is None:
            status_summary = summarize_enrichment_status(shard)
        _progress(progress, f"[{index}/{total}] Resuming: {source.name} text is complete")
    update_source_enrichment_status(
        state,
        filename=source.name,
        pending=needs_enrichment,
        status_counts=status_summary,
    )

    if (
        apply
        and not migration_changed
        and not needs_enrichment
        and _source_upload_is_current(manifest_entry, source.name, upload_checkpoint)
    ):
        _progress(progress, f"[{index}/{total}] Resuming: {source.name} is already uploaded")
        return _SourceTransactionResult(extracted=extracted, reused=reused, uploaded=False)

    uploaded_now = False
    if migration_changed or needs_enrichment or apply:
        published_source_names = None
        if apply:
            checkpoint_sources = upload_checkpoint.get("sources", {})
            if isinstance(checkpoint_sources, dict):
                published_source_names = {str(name) for name in checkpoint_sources}
            else:
                published_source_names = set()
            published_source_names.add(source.name)
        uploaded_now = _maybe_publish_enriched_shard(
            run_dir=run_dir,
            source=source,
            repo_id=repo_id,
            apply=apply,
            progress=progress,
            index=index,
            total=total,
            allow_bundle_only=not reused,
            published_source_names=published_source_names,
        )
    if uploaded_now:
        # The per-source entry is a typed dict literal that structurally
        # matches ``_SourceCheckpointEntry``; assigning via ``__setitem__``
        # avoids re-introducing a ``dict[str, object]`` cast.
        upload_checkpoint["sources"].__setitem__(
            source.name,
            {"polygon_sha256": str(manifest_entry["public_shard_sha256"])},
        )
    return _SourceTransactionResult(
        extracted=extracted,
        reused=reused,
        uploaded=uploaded_now,
    )


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


def _upload_public_shard(run_dir: Path, source: Path, repo_id: str) -> None:
    map_path = run_dir / "assets" / "geographic_polygon_density.png"
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
    _upload_public_shard(run_dir, source, repo_id)
    persist_successful_upload(run_dir, source)
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
    if parquet.schema_arrow.equals(POLYGON_PUBLIC_SCHEMA_V1_1, check_metadata=True):
        return True
    if not parquet.schema_arrow.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True):
        return True
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
