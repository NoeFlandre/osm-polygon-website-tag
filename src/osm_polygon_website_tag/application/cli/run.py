"""Run initialization, extraction, and full-workflow CLI adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from osm_polygon_website_tag.application.progress import ProgressReporter
from osm_polygon_website_tag.application.workflow import run_all
from osm_polygon_website_tag.pipeline.enrich import DEFAULT_FETCH_WORKERS
from osm_polygon_website_tag.pipeline.extraction import (
    DEFAULT_AREA_WORKERS,
    DEFAULT_MAX_IN_FLIGHT_AREAS,
    extract_pbf,
)
from osm_polygon_website_tag.runtime.run_state import (
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
    upsert_run_metadata,
)
from osm_polygon_website_tag.runtime.safety import assert_path_safe_against, normalize_path
from osm_polygon_website_tag.web.politeness import (
    DEFAULT_HOST_CONCURRENCY,
    DEFAULT_HOST_DELAY_SECONDS,
    HostPolicy,
)

from . import RepoId, RunDir, _configured_hf_dataset_repo, _json, _quiet


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


def run_all_command(
    source_root: Annotated[
        Path, typer.Option("--source-root", help="Read-only directory of source .osm.pbf files.")
    ],
    output_root: Annotated[
        Path, typer.Option("--output-root", help="Writable directory holding one run per --run-id.")
    ],
    run_id: Annotated[
        str, typer.Option("--run-id", help="Run directory name under --output-root.")
    ],
    repo_id: RepoId = None,
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
    host_concurrency: Annotated[
        int,
        typer.Option(
            "--host-concurrency",
            envvar="OSM_PWT_HOST_CONCURRENCY",
            help="Maximum simultaneous requests to one website host.",
        ),
    ] = DEFAULT_HOST_CONCURRENCY,
    host_delay_seconds: Annotated[
        float,
        typer.Option(
            "--host-delay-seconds",
            envvar="OSM_PWT_HOST_DELAY_SECONDS",
            help="Minimum seconds between request starts to one website host.",
        ),
    ] = DEFAULT_HOST_DELAY_SECONDS,
    detect_languages: Annotated[
        bool,
        typer.Option("--detect-languages", help="Run the opt-in GlotLID language stage."),
    ] = False,
) -> int:
    """Run or resume the complete PBF inventory."""
    if ensure_repo and not apply:
        raise ValueError("--ensure-repo requires --apply")
    progress = ProgressReporter(quiet=_quiet["enabled"])
    try:
        result = run_all(
            source_root=source_root,
            output_root=output_root,
            run_id=run_id,
            repo_id=_configured_hf_dataset_repo(repo_id),
            apply=apply,
            ensure_repo=ensure_repo,
            progress=progress,
            area_workers=area_workers,
            max_in_flight_areas=max_in_flight_areas,
            fetch_workers=fetch_workers,
            host_policy=HostPolicy(host_concurrency, host_delay_seconds),
            detect_languages=detect_languages,
        )
    except BaseException:
        progress.close(completed=False)
        raise
    progress.close(completed=result.complete)
    _json({**result.__dict__, "run_dir": str(result.run_dir)}, sort_keys=True)
    return 0


def init_command(
    output_root: Annotated[
        Path, typer.Option("--output-root", help="Writable directory holding one run per --run-id.")
    ],
    source_root: Annotated[
        Path, typer.Option("--source-root", help="Read-only directory of source .osm.pbf files.")
    ],
    expected_source: Annotated[
        list[Path],
        typer.Option(
            "--expected-source",
            help="Expected source PBF path; repeat once per source.",
        ),
    ],
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Run directory name [default: generated].")
    ] = None,
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
