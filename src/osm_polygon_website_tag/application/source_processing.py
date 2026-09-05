"""Per-source extraction, enrichment, language detection, and publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import pyarrow.parquet as pq

from osm_polygon_website_tag.application.inventory import source_bundle_is_complete
from osm_polygon_website_tag.application.resume_planner import (
    coerce_enrichment_status_summary,
    summarize_enrichment_status,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    schema_matches,
)
from osm_polygon_website_tag.contracts.text_schema import status_has_retryable_value
from osm_polygon_website_tag.pipeline.detect_languages import (
    detect_language_shard,
    shard_needs_language_detection,
)
from osm_polygon_website_tag.pipeline.enrich import EnrichmentResult, enrich_polygon_shard
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.pipeline.glotlid import LanguageDetector
from osm_polygon_website_tag.pipeline.public_schema_migration import migrate_public_shard
from osm_polygon_website_tag.publishing.incremental import (
    CheckpointV2,
    IncrementalPublishPlan,
    incremental_publish_changed_shard,
    persist_successful_upload,
)
from osm_polygon_website_tag.publishing.publish import _upload_folder
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.runtime.run_state import (
    RunState,
    SourceFingerprint,
    update_public_shard_metadata,
    update_source_enrichment_status,
)


@dataclass(frozen=True)
class SourceProcessingContext:
    """Immutable dependencies shared by one source-processing phase."""

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
    detect_languages: bool
    language_detector: LanguageDetector | None


@dataclass(frozen=True)
class SourcePhaseCounts:
    """Counts returned by one ordered source-processing phase."""

    extracted: int = 0
    reused: int = 0
    uploaded: int = 0


@dataclass(frozen=True)
class _SourceTransactionResult:
    extracted: bool
    reused: bool
    uploaded: bool


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


def process_sources(
    *,
    sources: Sequence[Path],
    ordered_sources: Sequence[Path],
    fingerprints_by_name: Mapping[str, SourceFingerprint],
    context: SourceProcessingContext,
    allow_extraction: bool,
) -> SourcePhaseCounts:
    """Process sources in the caller-provided order and aggregate phase counts."""
    counts = SourcePhaseCounts()
    for index, source in enumerate(ordered_sources, start=1):
        result = _process_source(
            source=source,
            fingerprint=fingerprints_by_name[source.name],
            context=context,
            index=index,
            total=len(sources),
            allow_extraction=allow_extraction,
        )
        counts = SourcePhaseCounts(
            extracted=counts.extracted + int(result.extracted),
            reused=counts.reused + int(result.reused),
            uploaded=counts.uploaded + int(result.uploaded),
        )
    return counts


def _process_source(
    *,
    source: Path,
    fingerprint: SourceFingerprint,
    context: SourceProcessingContext,
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
    language_changed = _detect_source_shard_if_needed(
        source=source,
        shard=bundle.shard,
        context=context,
        index=index,
        total=total,
    )
    uploaded = _publish_source_if_needed(
        source=source,
        context=context,
        index=index,
        total=total,
        reused=bundle.reused,
        migration_changed=migration_changed,
        needs_enrichment=decision.needs_enrichment,
        language_changed=language_changed,
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
    context: SourceProcessingContext,
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


def _extract_with_options(source: Path, context: SourceProcessingContext) -> None:
    kwargs: _ExtractionKwargs = {"run_state": context.state}
    if context.area_workers is not None:
        kwargs["area_workers"] = context.area_workers
    if context.max_in_flight_areas is not None:
        kwargs["max_in_flight_areas"] = context.max_in_flight_areas
    extract_pbf(source, context.run_dir, **kwargs)


def _migrate_public_shard_if_needed(
    source: Path,
    shard: Path,
    context: SourceProcessingContext,
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
    context: SourceProcessingContext,
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


def _detect_source_shard_if_needed(
    *,
    source: Path,
    shard: Path,
    context: SourceProcessingContext,
    index: int,
    total: int,
) -> bool:
    """Detect languages for one completed text shard when opt-in is enabled."""
    if not context.detect_languages:
        return False
    detector = context.language_detector
    if detector is None:
        if not shard_needs_language_detection(shard):
            return False
        raise ValueError("language detection requested without a detector")
    result = detect_language_shard(shard, detector=detector)
    if not result.changed:
        return False
    _progress(context.progress, f"[{index}/{total}] Detecting languages for {source.name}")
    update_public_shard_metadata(
        context.state,
        filename=source.name,
        row_count=result.row_count,
        shard_sha256=result.shard_sha256,
    )
    return result.changed


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


def _enrich_shard(shard: Path, context: SourceProcessingContext) -> EnrichmentResult:
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
    context: SourceProcessingContext,
    index: int,
    total: int,
    reused: bool,
    migration_changed: bool,
    needs_enrichment: bool,
    language_changed: bool = False,
) -> bool:
    if _source_upload_is_current_for_context(
        source=source,
        context=context,
        index=index,
        total=total,
        migration_changed=migration_changed,
        needs_enrichment=needs_enrichment,
        language_changed=language_changed,
    ):
        return False
    if not _source_requires_publication(
        context=context,
        migration_changed=migration_changed,
        needs_enrichment=needs_enrichment,
        language_changed=language_changed,
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
    context: SourceProcessingContext,
    index: int,
    total: int,
    migration_changed: bool,
    needs_enrichment: bool,
    language_changed: bool = False,
) -> bool:
    if not context.apply:
        return False
    if any((migration_changed, needs_enrichment, language_changed)):
        return False
    if not _source_upload_is_current(
        context.state.sources[source.name],
        source.name,
        context.upload_checkpoint,
    ):
        return False
    _progress(context.progress, f"[{index}/{total}] Resuming: {source.name} is already uploaded")
    return True


def _source_requires_publication(
    *,
    context: SourceProcessingContext,
    migration_changed: bool,
    needs_enrichment: bool,
    language_changed: bool = False,
) -> bool:
    return migration_changed or needs_enrichment or language_changed or context.apply


def _record_source_upload(
    source: Path,
    context: SourceProcessingContext,
    uploaded: bool,
) -> None:
    if not uploaded:
        return
    manifest_entry = context.state.sources[source.name]
    context.upload_checkpoint["sources"][source.name] = {
        "polygon_sha256": str(manifest_entry["public_shard_sha256"])
    }


def _published_source_names(
    context: SourceProcessingContext,
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
    manifest_entry: Mapping[str, object],
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
    map_path = run_dir / POLYGON_DENSITY_ASSET_REL_PATH
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


def _run_needs_enrichment(run_dir: Path) -> bool:
    for shard in sorted((run_dir / "polygons").glob("*.parquet")):
        if _shard_needs_enrichment(shard):
            return True
    return False


def _run_needs_language_detection(run_dir: Path) -> bool:
    """Return whether any public shard lacks a complete language result."""
    for shard in sorted((run_dir / "polygons").glob("*.parquet")):
        if shard_needs_language_detection(shard):
            return True
    return False


def _shard_needs_enrichment(shard: Path) -> bool:
    parquet = pq.ParquetFile(shard)
    if _schema_needs_enrichment(parquet):
        return True
    return _status_columns_need_enrichment(parquet)


def _schema_needs_enrichment(parquet: pq.ParquetFile) -> bool:
    schema = parquet.schema_arrow
    return schema_matches(schema, POLYGON_PUBLIC_SCHEMA_V1_1) or not (
        schema_matches(schema, POLYGON_PUBLIC_SCHEMA)
        or schema_matches(schema, POLYGON_PUBLIC_SCHEMA_V1_4)
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


__all__ = ["SourcePhaseCounts", "SourceProcessingContext", "process_sources"]
