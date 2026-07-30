"""Resumable end-to-end orchestration for a complete source inventory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow.parquet as pq

from .analyze import analyze_results
from .card import build_card
from .comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from .config import DEFAULT_HF_DATASET
from .enrich import enrich_polygon_shard
from .extraction import extract_pbf
from .finalize import finalize_run
from .hf_token import resolve_hf_token
from .polygon_schema import POLYGON_PUBLIC_SCHEMA, POLYGON_PUBLIC_SCHEMA_V1_1
from .publish import _upload_folder, create_repo, publish_to_hf
from .rejection_schema import REJECTION_SCHEMA
from .run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    STATUS_INITIALIZED,
    SourceFingerprint,
    expected_source_inventory,
    hash_shard,
    initialise_run,
    load_run,
    snapshot_source_fingerprint,
    transition_status,
    update_public_shard_metadata,
    upsert_run_metadata,
)
from .safety import assert_path_safe_against, normalize_path


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


def discover_sources(source_root: Path | str) -> list[Path]:
    """Return every PBF below ``source_root`` in deterministic order."""
    root = normalize_path(source_root)
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    sources = sorted(root.rglob("*.osm.pbf"), key=lambda path: path.relative_to(root).as_posix())
    if not sources:
        raise ValueError(f"no .osm.pbf files found below source root: {root}")
    names = [source.name for source in sources]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate source filenames are unsupported: {duplicates}")
    return sources


def run_all(
    *,
    source_root: Path | str,
    output_root: Path | str,
    run_id: str,
    repo_id: str = DEFAULT_HF_DATASET,
    apply: bool = False,
    ensure_repo: bool = False,
    progress: Callable[[str], None] | None = None,
) -> WorkflowResult:
    """Extract, checkpoint, analyze, finalize, and optionally publish all PBFs.

    Re-running the same command with the same ``run_id`` resumes from exact
    source and shard fingerprints. ``KeyboardInterrupt`` is deliberately not
    caught, so Ctrl-C returns control immediately without terminalizing the run.
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
        actual = [asdict(fingerprint) for fingerprint in fingerprints]
        if expected != sorted(actual, key=lambda item: item["filename"]):
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

    if apply and not resolve_hf_token():
        raise ValueError("run-all --apply requires Hugging Face environment/local credentials")
    if apply and ensure_repo:
        _progress(progress, f"Ensuring Hugging Face dataset repository {repo_id}")
        create_repo(repo_id=repo_id, exist_ok=True)

    extracted_count = 0
    skipped_count = 0
    uploaded_count = 0
    invocation_id = uuid4().hex
    if status in {STATUS_INITIALIZED, STATUS_EXTRACTING}:
        if status == STATUS_INITIALIZED:
            transition_status(state, STATUS_EXTRACTING)
        for index, (source, fingerprint) in enumerate(
            zip(sources, fingerprints, strict=True),
            start=1,
        ):
            if _source_bundle_is_complete(run_dir, state.sources.get(source.name), fingerprint):
                skipped_count += 1
                _progress(progress, f"[{index}/{len(sources)}] Resuming: {source.name} is complete")
            else:
                _progress(progress, f"[{index}/{len(sources)}] Extracting {source.name}")
                extract_pbf(source, run_dir, run_state=state)
                extracted_count += 1
        transition_status(state, STATUS_EXTRACTED)
        status = STATUS_EXTRACTED

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
        uploaded = _load_upload_checkpoint(run_dir)
        for index, source in enumerate(sources, start=1):
            fingerprint = fingerprints[index - 1]
            if not _source_bundle_is_complete(
                run_dir,
                state.sources.get(source.name),
                fingerprint,
            ):
                raise ValueError(f"cannot enrich incomplete source bundle: {source.name}")
            shard = _public_shard_path(run_dir, source)
            if not _shard_needs_enrichment(shard):
                _progress(
                    progress,
                    f"[{index}/{len(sources)}] Resuming: {source.name} text is complete",
                )
                if apply and _maybe_publish_enriched_shard(
                    run_dir=run_dir,
                    source=source,
                    uploaded=uploaded,
                    repo_id=repo_id,
                    apply=apply,
                    progress=progress,
                    index=index,
                    total=len(sources),
                ):
                    uploaded_count += 1
                continue
            _progress(progress, f"[{index}/{len(sources)}] Enriching {source.name}")
            result = enrich_polygon_shard(
                shard,
                cache_path=run_dir / "cache" / "website_text.sqlite3",
                invocation_id=invocation_id,
            )
            update_public_shard_metadata(
                state,
                filename=source.name,
                row_count=result.row_count,
                shard_sha256=result.shard_sha256,
            )
            if _maybe_publish_enriched_shard(
                run_dir=run_dir,
                source=source,
                uploaded=uploaded,
                repo_id=repo_id,
                apply=apply,
                progress=progress,
                index=index,
                total=len(sources),
            ):
                uploaded_count += 1
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


def _source_bundle_is_complete(
    run_dir: Path,
    manifest: dict[str, Any] | None,
    fingerprint: SourceFingerprint,
) -> bool:
    if manifest is None:
        return False
    if any(manifest.get(key) != value for key, value in asdict(fingerprint).items()):
        return False
    stem = fingerprint.short_id()
    paths_and_contracts = (
        (
            run_dir / "polygons" / f"{stem}.parquet",
            (POLYGON_PUBLIC_SCHEMA_V1_1, POLYGON_PUBLIC_SCHEMA),
            "public_row_count",
            "public_shard_sha256",
        ),
        (
            run_dir / "analysis_observations" / f"{stem}.parquet",
            COMPARISON_OBSERVATION_SCHEMA,
            "observation_row_count",
            "observation_shard_sha256",
        ),
        (
            run_dir / "rejections" / f"{stem}.parquet",
            REJECTION_SCHEMA,
            "rejection_count",
            "rejection_shard_sha256",
        ),
    )
    for path, schema_contract, count_key, hash_key in paths_and_contracts:
        if not path.is_file():
            return False
        parquet = pq.ParquetFile(path)
        schemas = schema_contract if isinstance(schema_contract, tuple) else (schema_contract,)
        if not any(parquet.schema_arrow.equals(schema, check_metadata=True) for schema in schemas):
            return False
        if parquet.metadata.num_rows != manifest.get(count_key):
            return False
        if hash_shard(path) != manifest.get(hash_key):
            return False
    return True


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _public_shard_hash(run_dir: Path, source: Path) -> str:
    return hash_shard(_public_shard_path(run_dir, source))


def _public_shard_path(run_dir: Path, source: Path) -> Path:
    return run_dir / "polygons" / f"{source.name.removesuffix('.osm.pbf')}.parquet"


def _upload_public_shard(run_dir: Path, source: Path, repo_id: str) -> None:
    shard = _public_shard_path(run_dir, source)
    _upload_folder(
        run_dir,
        repo_id=repo_id,
        repo_kind="dataset",
        artifact_paths=[shard, run_dir / "README.md", run_dir / "dataset.yaml"],
    )


def _maybe_publish_enriched_shard(
    *,
    run_dir: Path,
    source: Path,
    uploaded: dict[str, object],
    repo_id: str,
    apply: bool,
    progress: Callable[[str], None] | None,
    index: int,
    total: int,
) -> bool:
    """Build the card, compute the checkpoint, and conditionally upload.

    Owns the "build card -> checkpoint -> upload -> persist checkpoint"
    transaction for one enriched shard. Always rebuilds the card so that
    README.md/dataset.yaml reflect the current shard state. Returns
    ``True`` iff a new incremental upload was performed; the caller
    updates ``uploaded_count`` from this return value.
    """
    build_card(run_dir)
    checkpoint = _upload_checkpoint_entry(run_dir, source)
    prior = uploaded.get(source.name)
    if not apply:
        return False
    if isinstance(prior, dict) and prior.get("polygon_sha256") == checkpoint["polygon_sha256"]:
        return False
    _progress(
        progress,
        f"[{index}/{total}] Uploading enriched shard and recomputed card",
    )
    _upload_public_shard(run_dir, source, repo_id)
    uploaded[source.name] = checkpoint
    _write_upload_checkpoint(run_dir, uploaded)
    return True


def _checkpoint_path(run_dir: Path) -> Path:
    return run_dir / "manifests" / "uploaded_polygons.json"


def _load_upload_checkpoint(run_dir: Path) -> dict[str, object]:
    path = _checkpoint_path(run_dir)
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid uploaded polygon checkpoint")
    return value


def _write_upload_checkpoint(run_dir: Path, uploaded: dict[str, object]) -> None:
    path = _checkpoint_path(run_dir)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(uploaded, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _upload_checkpoint_entry(run_dir: Path, source: Path) -> dict[str, str]:
    return {
        "polygon_sha256": _public_shard_hash(run_dir, source),
        "readme_sha256": hash_shard(run_dir / "README.md"),
        "dataset_yaml_sha256": hash_shard(run_dir / "dataset.yaml"),
    }


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
        for row in batch.to_pylist():
            if row["website_text_status"] not in {"success", "absent"}:
                return True
            if row["contact_website_text_status"] not in {"success", "absent"}:
                return True
    return False


__all__ = ["WorkflowResult", "discover_sources", "run_all"]
