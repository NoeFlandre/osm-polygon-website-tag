"""Review-gated local migration of pre-map completed runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.finalize import FinalizationReport, replace_receipt_atomic
from osm_polygon_website_tag.reporting.verify import (
    VerificationReport,
    verify_results,
    verify_results_modern,
)
from osm_polygon_website_tag.runtime.run_state import STATUS_CARD_BUILT, STATUS_COMPLETE, load_run

REFRESH_ALLOWED_STATUSES = frozenset({STATUS_CARD_BUILT, STATUS_COMPLETE})


@dataclass(frozen=True)
class PreflightReport:
    """Result of the non-mutating refresh preflight."""

    ok: bool
    errors: list[str] = field(default_factory=list)


def preflight_legacy_refresh(run_dir: Path | str) -> PreflightReport:
    """Check the local shape needed before rebuilding a card/map bundle."""
    root = Path(run_dir)
    state = load_run(root)
    status = state.metadata.get("status")
    if status not in REFRESH_ALLOWED_STATUSES:
        return PreflightReport(
            False,
            [f"refresh-card requires card_built or complete state, got {status!r}"],
        )
    errors: list[str] = []
    for relative in (
        "manifests/run.json",
        "manifests/sources.json",
        "manifests/expected_sources.json",
        "README.md",
        "dataset.yaml",
    ):
        if not (root / relative).is_file():
            errors.append(f"missing refresh prerequisite: {relative}")
    for directory in ("polygons", "analysis_observations", "rejections", "analysis"):
        if not (root / directory).is_dir():
            errors.append(f"missing refresh directory: {directory}")
    for path in root.glob("polygons/*.parquet"):
        try:
            pq.ParquetFile(path)
        except Exception as exc:
            errors.append(f"unreadable public shard {path}: {exc}")
    receipt = root / "manifests" / "completion_receipt.json"
    if receipt.is_file():
        try:
            payload = json.loads(receipt.read_text())
            if not isinstance(payload, dict):
                errors.append("completion receipt is not a JSON object")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid completion receipt: {exc}")
    return PreflightReport(not errors, errors)


def refresh_card_run(run_dir: Path | str) -> FinalizationReport:
    """Rebuild and strictly finalize a local map/card bundle without source work."""
    root = Path(run_dir)
    preflight = preflight_legacy_refresh(root)
    if not preflight.ok:
        return FinalizationReport(
            False,
            {},
            VerificationReport(False, preflight.errors),
        )
    build_card(root)
    modern = verify_results_modern(root)
    if not modern.ok:
        return FinalizationReport(False, {}, modern)
    replace_receipt_atomic(root)
    return _finalize_after_receipt(root)


def _finalize_after_receipt(root: Path) -> FinalizationReport:
    """Validate the new receipt and complete/rebind the run lifecycle."""
    from osm_polygon_website_tag.reporting.finalize import finalize_run

    strict = verify_results(root)
    if not strict.ok:
        return FinalizationReport(False, {}, strict)
    return finalize_run(root)


__all__ = ["PreflightReport", "preflight_legacy_refresh", "refresh_card_run"]
