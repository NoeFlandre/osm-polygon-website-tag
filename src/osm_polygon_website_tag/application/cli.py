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
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.publishing.publish import (
    build_publish_plan,
    create_repo,
    publish_to_hf,
)
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
from osm_polygon_website_tag.reporting.finalize import finalize_run
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_ENRICHED,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    STATUS_INITIALIZED,
    expected_source_inventory,
    initialise_run,
    load_run,
    snapshot_source_fingerprint,
    source_inventory_matches,
    transition_status,
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
) -> int:
    """Extract one source PBF."""
    state_path = run_dir / "manifests" / "run.json"
    if not state_path.is_file():
        raise ValueError("extract requires a run created by the init command")
    state = load_run(run_dir)
    fingerprint = snapshot_source_fingerprint(pbf_path)
    expected = expected_source_inventory(run_dir)
    if {
        "filename": fingerprint.filename,
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
    } not in expected:
        raise ValueError(f"source is not in exact expected inventory: {pbf_path.name}")
    status = state.metadata.get("status")
    if status == STATUS_INITIALIZED:
        transition_status(state, STATUS_EXTRACTING)
    elif status != STATUS_EXTRACTING:
        raise ValueError(f"extract requires initialized/extracting state, got {status!r}")
    extract_pbf(pbf_path, run_dir, run_state=state)
    if source_inventory_matches(run_dir):
        transition_status(state, STATUS_EXTRACTED)
    return 0


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


@app.command("finalize-run")
def finalize_command(run_dir: RunDir) -> int:
    """Finalize a verified run."""
    report = finalize_run(run_dir)
    _json({"ok": report.ok, "digest": report.receipt.get("manifest_digest")})
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
        )
    except BaseException:
        progress.close(completed=False)
        raise
    progress.close(completed=result.complete)
    _json({**result.__dict__, "run_dir": str(result.run_dir)}, sort_keys=True)
    return 0


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
