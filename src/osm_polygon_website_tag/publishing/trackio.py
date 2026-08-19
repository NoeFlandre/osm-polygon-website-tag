"""Publish one finalized dataset snapshot to a Hugging Face Trackio Space.

Trackio is intentionally an optional integration.  The normal extraction and
publication pipeline does not import it or require it to be installed.  The
``publish-trackio`` command loads the package only when ``--apply`` is used,
so a dry run remains fully local and deterministic.  Apply mode writes one
local run and freezes it into a public, read-only static Space; it never
starts a live dashboard server.

Metrics are projected from :class:`~osm_polygon_website_tag.reporting.card_stats.CardStats`,
the same artifact-derived source used to render the public dataset card.  A
completion receipt is required before anything can be sent to Trackio; this
prevents a partial local run from being presented as a public snapshot.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.reporting.card_stats import CardStats, compute_card_stats
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.config import (
    DEFAULT_HF_DATASET,
    DEFAULT_TRACKIO_PROJECT,
    DEFAULT_TRACKIO_SPACE,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MetricValue = int | float
TRACKIO_HEADLINE_METRICS = (
    "dataset_public_polygon_rows",
    "polygons_with_extracted_text",
    "total_extracted_words",
    "website_text_coverage",
    "contact_website_text_coverage",
    "dataset_source_shards",
    "occupied_h3_cells",
)


@dataclass(frozen=True)
class TrackioSnapshot:
    """Artifact-derived values for one stable Trackio run."""

    run_name: str
    manifest_digest: str
    dataset_repo: str
    metrics: Mapping[str, MetricValue]
    metric_source: str = "completion_receipt_and_public_parquets"

    @property
    def config(self) -> dict[str, str]:
        """Return the non-sensitive Trackio run configuration."""
        return {
            "dataset_repo": self.dataset_repo,
            "dataset_snapshot": self.manifest_digest,
            "metric_source": self.metric_source,
        }


def build_trackio_snapshot(
    run_dir: Path | str,
    *,
    dataset_repo: str = DEFAULT_HF_DATASET,
) -> TrackioSnapshot:
    """Build metrics from a complete, receipt-bound local run.

    The function never contacts Hugging Face and never reads the source PBFs.
    It requires the final run state and completion receipt so callers cannot
    accidentally publish an incomplete local view as a public snapshot.
    """
    root = Path(run_dir)
    _require_complete_run(root)
    manifest_digest = _read_manifest_digest(root)
    verification = verify_results(root)
    if not verification.ok:
        details = "; ".join(verification.errors[:3])
        raise ValueError(f"Trackio metrics require a verified run: {details}")
    stats = compute_card_stats(root)
    metrics = _metrics_from_card_stats(stats)
    return TrackioSnapshot(
        run_name=f"dataset-{manifest_digest[:16]}",
        manifest_digest=manifest_digest,
        dataset_repo=dataset_repo,
        metrics=metrics,
    )


def publish_trackio_snapshot(
    snapshot: TrackioSnapshot,
    *,
    space_id: str = DEFAULT_TRACKIO_SPACE,
    project: str = DEFAULT_TRACKIO_PROJECT,
) -> dict[str, str | None]:
    """Log ``snapshot`` to a public Trackio Space and return safe metadata.

    Trackio resolves Hugging Face credentials through its normal environment or
    local credential store.  No credential is accepted as an argument or
    included in the returned metadata.  The stable run name and ``resume``
    policy make rerunning the same receipt update one Trackio run instead of
    creating an unbounded collection of duplicate runs.
    """
    _validate_trackio_destination(space_id, project)
    trackio = _load_trackio()
    run = _log_trackio_snapshot(trackio, snapshot, project=project)
    _sync_trackio(trackio, project=project, space_id=space_id)
    return _trackio_result(space_id, project, snapshot, run)


def _validate_trackio_destination(space_id: str, project: str) -> None:
    """Validate the public Trackio identifiers before importing its SDK."""
    if not space_id.strip():
        raise ValueError("Trackio space_id must not be empty")
    if not project.strip():
        raise ValueError("Trackio project must not be empty")


def _load_trackio() -> Any:
    """Import the optional Trackio SDK with an actionable error."""
    try:
        return importlib.import_module("trackio")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Trackio publishing requires the optional 'trackio' package; "
            "run with `uv run --with trackio ...`"
        ) from exc


def _log_trackio_snapshot(trackio: Any, snapshot: TrackioSnapshot, *, project: str) -> Any:
    """Log one receipt-bound run and always finish an initialized SDK run."""
    initialized = False
    run: Any = None
    try:
        run = trackio.init(
            project=project,
            name=snapshot.run_name,
            config=snapshot.config,
            resume="allow",
            private=False,
            auto_log_cpu=False,
            auto_log_gpu=False,
        )
        initialized = True
        trackio.log(dict(snapshot.metrics))
    finally:
        if initialized:
            trackio.finish()
    return run


def _sync_trackio(trackio: Any, *, project: str, space_id: str) -> None:
    """Publish the logged project to a public static Trackio Space."""
    trackio.sync(project=project, space_id=space_id, private=False, sdk="static")


def _trackio_result(
    space_id: str, project: str, snapshot: TrackioSnapshot, run: Any
) -> dict[str, str | None]:
    """Return safe remote identifiers without exposing credentials."""
    run_id = getattr(run, "id", None)
    return {
        "space_id": space_id,
        "project": project,
        "run_name": snapshot.run_name,
        "run_id": run_id if isinstance(run_id, str) else None,
    }


def _require_complete_run(root: Path) -> None:
    metadata_path = root / "manifests" / "run.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Trackio metrics require a readable run metadata file") from exc
    if not isinstance(metadata, dict) or metadata.get("status") != "complete":
        raise ValueError("Trackio metrics require a run in complete status")


def _read_manifest_digest(root: Path) -> str:
    receipt_path = root / "manifests" / "completion_receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Trackio metrics require a readable completion receipt") from exc
    digest = receipt.get("manifest_digest") if isinstance(receipt, dict) else None
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("completion receipt has an invalid manifest digest")
    return digest


def _metrics_from_card_stats(stats: CardStats) -> dict[str, MetricValue]:
    website_successes = stats.website_text_success_count
    contact_successes = stats.contact_website_text_success_count
    total_words = stats.website_total_words + stats.contact_website_total_words
    metrics = {
        "dataset_public_polygon_rows": stats.public_row_count,
        "dataset_source_shards": stats.sources_count,
        "polygons_with_extracted_text": stats.polygons_with_any_text,
        "total_extracted_words": total_words,
        "website_text_coverage": _ratio(website_successes, stats.website_urls_present),
        "contact_website_text_coverage": _ratio(
            contact_successes, stats.contact_website_urls_present
        ),
        "occupied_h3_cells": stats.occupied_h3_cell_count,
    }
    return {name: metrics[name] for name in TRACKIO_HEADLINE_METRICS}


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


__all__ = [
    "TrackioSnapshot",
    "build_trackio_snapshot",
    "publish_trackio_snapshot",
]
