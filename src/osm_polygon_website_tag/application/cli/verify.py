"""Analysis, card, verification, and finalization CLI adapters."""

from __future__ import annotations

import typer

from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
from osm_polygon_website_tag.reporting.finalize import finalize_run, finalize_snapshot
from osm_polygon_website_tag.reporting.geometry_stats import (
    compute_geometry_stats,
    render_geometry_stats,
)
from osm_polygon_website_tag.reporting.repair import refresh_card_run
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_ENRICHED,
    load_run,
    transition_status,
)

from . import RunDir, _json


def refresh_card_command(run_dir: RunDir) -> int:
    """Rebuild the local H3 map/card and migrate its completion receipt."""
    report = refresh_card_run(run_dir)
    _json({"ok": report.ok, "errors": report.verification.errors})
    if not report.ok:
        raise typer.Exit(code=1)
    return 0


def geometry_stats_command(run_dir: RunDir) -> int:
    """Recompute and print polygon geometry statistics for a run."""
    typer.echo(render_geometry_stats(compute_geometry_stats(run_dir)), nl=False)
    return 0


def analyze_command(run_dir: RunDir) -> int:
    """Run the analyzer."""
    state = load_run(run_dir)
    if state.metadata.get("status") != STATUS_ENRICHED:
        raise ValueError("analyze-results requires enriched state; use run-all for enrichment")
    summary = analyze_results(run_dir)
    transition_status(state, STATUS_ANALYZED)
    _json(summary.__dict__)
    return 0


def verify_command(run_dir: RunDir) -> int:
    """Verify a run without mutating it."""
    report = verify_results(run_dir)
    _json({"ok": report.ok, "errors": report.errors})
    if not report.ok:
        raise typer.Exit(code=1)
    return 0


def finalize_command(run_dir: RunDir) -> int:
    """Finalize a verified run."""
    report = finalize_run(run_dir)
    _json({"ok": report.ok, "digest": report.receipt.get("manifest_digest")})
    if not report.ok:
        raise typer.Exit(code=1)
    return 0


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


def card_command(run_dir: RunDir) -> int:
    """Build the artifact-derived dataset card."""
    state = load_run(run_dir)
    if state.metadata.get("status") != STATUS_ANALYZED:
        raise ValueError("build-card requires analyzed state")
    path = build_card(run_dir)
    transition_status(state, STATUS_CARD_BUILT)
    typer.echo(str(path))
    return 0


def card_stats_command(run_dir: RunDir) -> int:
    """Recompute and print dataset-card statistics."""
    stats = compute_card_stats(run_dir)
    _json(stats.__dict__)
    return 0
