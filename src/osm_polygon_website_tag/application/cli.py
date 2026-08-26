"""Typed command-line interface for the resumable dataset pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from osm_polygon_website_tag.application.progress import ProgressReporter
from osm_polygon_website_tag.application.workflow import run_all
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.pipeline.detect_languages import (
    detect_language_shard,
    shard_needs_language_detection,
)
from osm_polygon_website_tag.pipeline.enrich import DEFAULT_FETCH_WORKERS
from osm_polygon_website_tag.pipeline.extraction import (
    DEFAULT_AREA_WORKERS,
    DEFAULT_MAX_IN_FLIGHT_AREAS,
    extract_pbf,
)
from osm_polygon_website_tag.pipeline.glotlid import load_glotlid_detector
from osm_polygon_website_tag.publishing.publish import (
    build_publish_plan,
    create_repo,
    publish_to_hf,
)
from osm_polygon_website_tag.publishing.trackio import (
    build_trackio_snapshot,
    publish_trackio_snapshot,
)
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
from osm_polygon_website_tag.reporting.finalize import finalize_run, finalize_snapshot
from osm_polygon_website_tag.reporting.repair import refresh_card_run
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.config import (
    DEFAULT_HF_DATASET,
    DEFAULT_TRACKIO_PROJECT,
    DEFAULT_TRACKIO_SPACE,
)
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
    source_inventory_matches,
    transition_status,
    update_public_shard_metadata,
    upsert_run_metadata,
)
from osm_polygon_website_tag.runtime.safety import assert_path_safe_against, normalize_path

app = typer.Typer(
    name="osm-polygon-website-tag",
    help="Analyze and publish OSM polygons carrying website tags.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
_error_console = Console(stderr=True, markup=False, highlight=False)

RunDir = Annotated[Path, typer.Option("--run-dir", help="Existing run directory.")]
RepoId = Annotated[str, typer.Option("--repo-id", help="Hugging Face dataset repository.")]


def _json(payload: Any, *, sort_keys: bool = False) -> None:
    typer.echo(json.dumps(payload, default=str, indent=2, sort_keys=sort_keys))


@app.command("init")
def init_command(
    output_root: Annotated[Path, typer.Option("--output-root")],
    source_root: Annotated[Path, typer.Option("--source-root")],
    expected_source: Annotated[
        list[Path],
        typer.Option(
            "--expected-source",
            help="Expected source PBF path; repeat once per source.",
        ),
    ],
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
) -> int:
    """Initialise a new run directory."""
    normalized_source_root = normalize_path(source_root)
    normalized_output_root = assert_path_safe_against(output_root, normalized_source_root)
    sources = [normalize_path(source) for source in expected_source]
    for source in sources:
        if not source.is_relative_to(normalized_source_root):
            raise ValueError(f"expected source is outside source root: {source}")
    fingerprints = [snapshot_source_fingerprint(source) for source in expected_source]
    run_dir, _ = initialise_run(
        normalized_output_root,
        run_id=run_id,
        expected_sources=fingerprints,
    )
    state = load_run(run_dir)
    upsert_run_metadata(state, {"source_root": str(normalized_source_root)})
    typer.echo(str(run_dir))
    return 0


@app.command("extract")
def extract_command(
    pbf_path: Annotated[Path, typer.Argument(help="Source .osm.pbf file.")],
    run_dir: RunDir,
    area_workers: Annotated[
        int,
        typer.Option("--area-workers", help="Bounded geometry workers for this PBF."),
    ] = DEFAULT_AREA_WORKERS,
    max_in_flight_areas: Annotated[
        int,
        typer.Option(
            "--max-in-flight-areas",
            help="Maximum queued area payloads for this PBF.",
        ),
    ] = DEFAULT_MAX_IN_FLIGHT_AREAS,
) -> int:
    """Extract one source PBF."""
    state_path = run_dir / "manifests" / "run.json"
    if not state_path.is_file():
        raise ValueError("extract requires a run created by the init command")
    state = load_run(run_dir)
    fingerprint = snapshot_source_fingerprint(pbf_path)
    _validate_expected_extract_source(run_dir, fingerprint, pbf_path)
    _prepare_extract_status(state)
    extract_pbf(
        pbf_path,
        run_dir,
        run_state=state,
        area_workers=area_workers,
        max_in_flight_areas=max_in_flight_areas,
    )
    if source_inventory_matches(run_dir):
        transition_status(state, STATUS_EXTRACTED)
    return 0


def _validate_expected_extract_source(
    run_dir: Path, fingerprint: SourceFingerprint, pbf_path: Path
) -> None:
    """Require the exact source identity recorded during run initialization."""
    expected = expected_source_inventory(run_dir)
    candidate = {
        "filename": fingerprint.filename,
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
    }
    if candidate not in expected:
        raise ValueError(f"source is not in exact expected inventory: {pbf_path.name}")


def _prepare_extract_status(state: RunState) -> None:
    """Transition an initialized run into extraction or reject other states."""
    status = state.metadata.get("status")
    if status == STATUS_INITIALIZED:
        transition_status(state, STATUS_EXTRACTING)
    elif status != STATUS_EXTRACTING:
        raise ValueError(f"extract requires initialized/extracting state, got {status!r}")


@app.command("analyze-results")
def analyze_command(run_dir: RunDir) -> int:
    """Run the analyzer."""
    state = load_run(run_dir)
    if state.metadata.get("status") != STATUS_ENRICHED:
        raise ValueError("analyze-results requires enriched state; use run-all for enrichment")
    summary = analyze_results(run_dir)
    transition_status(state, STATUS_ANALYZED)
    _json(summary.__dict__)
    return 0


@app.command("build-card")
def card_command(run_dir: RunDir) -> int:
    """Build the artifact-derived dataset card."""
    state = load_run(run_dir)
    if state.metadata.get("status") != STATUS_ANALYZED:
        raise ValueError("build-card requires analyzed state")
    path = build_card(run_dir)
    transition_status(state, STATUS_CARD_BUILT)
    typer.echo(str(path))
    return 0


@app.command("verify-results")
def verify_command(run_dir: RunDir) -> int:
    """Verify a run without mutating it."""
    report = verify_results(run_dir)
    _json({"ok": report.ok, "errors": report.errors})
    if not report.ok:
        raise typer.Exit(code=1)
    return 0


@app.command("refresh-card")
def refresh_card_command(run_dir: RunDir) -> int:
    """Rebuild the local H3 map/card and migrate its completion receipt."""
    report = refresh_card_run(run_dir)
    _json({"ok": report.ok, "errors": report.verification.errors})
    if not report.ok:
        raise typer.Exit(code=1)
    return 0


@app.command("finalize-run")
def finalize_command(run_dir: RunDir) -> int:
    """Finalize a verified run."""
    report = finalize_run(run_dir)
    _json({"ok": report.ok, "digest": report.receipt.get("manifest_digest")})
    if not report.ok:
        raise typer.Exit(code=1)
    return 0


@app.command("finalize-snapshot")
def finalize_snapshot_command(run_dir: RunDir) -> int:
    """Finalize a user-frozen snapshot without website enrichment."""
    report = finalize_snapshot(run_dir)
    _json(
        {
            "digest": report.receipt.get("manifest_digest"),
            "errors": report.verification.errors,
            "ok": report.ok,
        }
    )
    if not report.ok:
        raise typer.Exit(code=1)
    return 0


@app.command("publish-plan")
def publish_plan_command(
    run_dir: RunDir,
    repo_id: RepoId = DEFAULT_HF_DATASET,
) -> int:
    """List the publication plan."""
    plan = build_publish_plan(run_dir, repo_id=repo_id)
    _json(
        {
            "repo_id": plan.repo_id,
            "artifact_count": len(plan.artifact_paths),
            "readme": str(plan.readme_path) if plan.readme_path else None,
        }
    )
    return 0


@app.command("publish")
def publish_command(
    run_dir: RunDir,
    repo_id: RepoId = DEFAULT_HF_DATASET,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually upload (default: dry-run)."),
    ] = False,
) -> int:
    """Publish or dry-run a complete dataset."""
    plan = publish_to_hf(run_dir, repo_id=repo_id, dry_run=not apply)
    _json({"dry_run": not apply, "artifact_count": len(plan.artifact_paths)})
    return 0


@app.command("create-repo")
def create_repo_command(
    repo_id: Annotated[str, typer.Option("--repo-id")],
    exist_ok: Annotated[bool, typer.Option("--exist-ok")] = False,
) -> int:
    """Create the Hugging Face dataset repository."""
    repo = create_repo(repo_id=repo_id, exist_ok=exist_ok)
    typer.echo(repo)
    return 0


@app.command("card-stats")
def card_stats_command(run_dir: RunDir) -> int:
    """Recompute and print dataset-card statistics."""
    stats = compute_card_stats(run_dir)
    _json(stats.__dict__)
    return 0


@app.command("publish-trackio")
def publish_trackio_command(
    run_dir: RunDir,
    space_id: Annotated[
        str,
        typer.Option("--space-id", help="Public Hugging Face Trackio Space."),
    ] = DEFAULT_TRACKIO_SPACE,
    project: Annotated[
        str,
        typer.Option("--project", help="Trackio project name."),
    ] = DEFAULT_TRACKIO_PROJECT,
    dataset_repo: Annotated[
        str,
        typer.Option("--dataset-repo", help="Dataset repository represented by the metrics."),
    ] = DEFAULT_HF_DATASET,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually create/update the public Trackio Space."),
    ] = False,
) -> int:
    """Preview or publish metrics for one finalized dataset snapshot."""
    snapshot = build_trackio_snapshot(run_dir, dataset_repo=dataset_repo)
    remote = (
        publish_trackio_snapshot(snapshot, space_id=space_id, project=project) if apply else None
    )
    _json(
        {
            "dry_run": not apply,
            "space_id": space_id,
            "project": project,
            "run_name": snapshot.run_name,
            "manifest_digest": snapshot.manifest_digest,
            "metrics": snapshot.metrics,
            "remote": remote,
        },
        sort_keys=True,
    )
    return 0


@app.command("run-all")
def run_all_command(
    source_root: Annotated[Path, typer.Option("--source-root")],
    output_root: Annotated[Path, typer.Option("--output-root")],
    run_id: Annotated[str, typer.Option("--run-id")],
    repo_id: RepoId = DEFAULT_HF_DATASET,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Upload after each PBF and at completion."),
    ] = False,
    ensure_repo: Annotated[
        bool,
        typer.Option(
            "--ensure-repo",
            help="Create the HF dataset repo if needed (only with --apply).",
        ),
    ] = False,
    area_workers: Annotated[
        int,
        typer.Option("--area-workers", help="Bounded geometry workers per PBF."),
    ] = DEFAULT_AREA_WORKERS,
    max_in_flight_areas: Annotated[
        int,
        typer.Option(
            "--max-in-flight-areas",
            help="Maximum queued area payloads per PBF.",
        ),
    ] = DEFAULT_MAX_IN_FLIGHT_AREAS,
    fetch_workers: Annotated[
        int,
        typer.Option("--fetch-workers", help="Bounded concurrent URL fetch workers."),
    ] = DEFAULT_FETCH_WORKERS,
    detect_languages: Annotated[
        bool,
        typer.Option("--detect-languages", help="Run the opt-in GlotLID language stage."),
    ] = False,
) -> int:
    """Run or resume the complete PBF inventory."""
    if ensure_repo and not apply:
        raise ValueError("--ensure-repo requires --apply")
    progress = ProgressReporter()
    try:
        result = run_all(
            source_root=source_root,
            output_root=output_root,
            run_id=run_id,
            repo_id=repo_id,
            apply=apply,
            ensure_repo=ensure_repo,
            progress=progress,
            area_workers=area_workers,
            max_in_flight_areas=max_in_flight_areas,
            fetch_workers=fetch_workers,
            detect_languages=detect_languages,
        )
    except BaseException:
        progress.close(completed=False)
        raise
    progress.close(completed=result.complete)
    _json({**result.__dict__, "run_dir": str(result.run_dir)}, sort_keys=True)
    return 0


@app.command("detect-languages")
def detect_languages_command(run_dir: RunDir) -> int:
    """Detect GlotLID languages for every completed text shard."""
    normalized_run_dir = assert_seagate_path(run_dir, label="run directory")
    state = load_run(normalized_run_dir)
    paths = sorted((normalized_run_dir / "polygons").glob("*.parquet"))
    _validate_language_shard_membership(state, paths)
    _reject_frozen_language_run(state)
    needed = [path for path in paths if shard_needs_language_detection(path)]
    if not needed:
        _json({"changed_shards": 0, "run_dir": str(normalized_run_dir)}, sort_keys=True)
        return 0
    _prepare_language_command_state(state)
    model_cache = glotlid_model_cache_dir()
    assert_seagate_path(model_cache, label="GlotLID model cache")
    detector = load_glotlid_detector(model_cache)
    changed = 0
    for shard in needed:
        result = detect_language_shard(shard, detector=detector)
        update_public_shard_metadata(
            state,
            filename=f"{shard.stem}.osm.pbf",
            row_count=result.row_count,
            shard_sha256=result.shard_sha256,
        )
        changed += int(result.changed)
    _finish_language_command_state(state)
    _json({"changed_shards": changed, "run_dir": str(normalized_run_dir)}, sort_keys=True)
    return 0


def _validate_language_shard_membership(state: RunState, paths: list[Path]) -> None:
    """Require every public shard to belong to the run's source manifest."""
    for path in paths:
        source_name = f"{path.stem}.osm.pbf"
        if source_name not in state.sources:
            raise ValueError(f"language shard is not in the source manifest: {path.name}")


def _reject_frozen_language_run(state: RunState) -> None:
    """Reject mutation of a snapshot explicitly frozen by the operator."""
    if (
        state.metadata.get("status") == STATUS_COMPLETE
        and state.metadata.get("snapshot_status") == "done"
    ):
        raise ValueError("cannot add languages to a frozen snapshot")


def _prepare_language_command_state(state: RunState) -> None:
    """Enter the resumable language stage or reject an unsuitable run."""
    _reject_frozen_language_run(state)
    status = state.metadata.get("status")
    if status in {STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}:
        transition_status(state, STATUS_ENRICHING)
    elif status not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("detect-languages requires an extracted/enriched run")


def _finish_language_command_state(state: RunState) -> None:
    """Complete the language stage after every shard was promoted."""
    if state.metadata.get("status") == STATUS_ENRICHING:
        transition_status(state, STATUS_ENRICHED)


def main(argv: list[str] | None = None) -> int:
    """Run the Typer app while preserving the historical integer API."""
    try:
        app(
            args=argv,
            prog_name="osm-polygon-website-tag",
            standalone_mode=True,
        )
    except SystemExit as exc:
        return int(exc.code or 0)
    except ValueError as exc:
        _error_console.print(f"error: {exc}")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["app", "main"]
