"""Finalize a verified run and bind every publishable artifact."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.artifact_inventory import hash_file, publishable_paths
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.verify import (
    VerificationReport,
    verify_results,
    verify_results_modern,
)
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    STATUS_VERIFIED,
    load_run,
    transition_status,
)


@dataclass
class FinalizationReport:
    """Result of :func:`finalize_run`."""

    ok: bool
    receipt: dict[str, Any]
    verification: VerificationReport


_FROZEN_ALLOWED_STATUSES = frozenset(
    {
        STATUS_EXTRACTING,
        STATUS_EXTRACTED,
        STATUS_ENRICHING,
        STATUS_ENRICHED,
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_COMPLETE,
    }
)


def finalize_snapshot(run_dir: Path | str) -> FinalizationReport:
    """Finalize an explicitly frozen snapshot without running enrichment.

    The run must carry ``snapshot_status=done`` and contain no unfinished
    ``pending`` text statuses. Existing non-success outcomes such as
    ``fetch_error`` and ``unsafe_url`` are preserved as part of the frozen
    snapshot. Analysis, card/map generation, verification, and receipt
    creation are performed from the existing Parquets only.
    """
    root = Path(run_dir)
    state = load_run(root)
    status = state.metadata.get("status")
    preflight_error = _snapshot_preflight_error(state.metadata, status)
    if preflight_error is not None:
        return _failed_snapshot_report(preflight_error)

    preflight = verify_results_modern(root)
    if not preflight.ok:
        return FinalizationReport(False, {}, preflight)
    unfinished = _unfinished_text_status_errors(root)
    if unfinished:
        return FinalizationReport(
            False,
            {},
            VerificationReport(False, unfinished, preflight.checked_shards),
        )

    _advance_snapshot_state(root, state)
    return finalize_run(root)


def _snapshot_preflight_error(metadata: dict[str, Any], status: object) -> str | None:
    """Return the user-facing reason a run cannot be frozen."""
    if metadata.get("snapshot_status") != "done":
        return "snapshot finalization requires snapshot_status='done'"
    if status not in _FROZEN_ALLOWED_STATUSES:
        return f"cannot freeze run from status {status!r}"
    return None


def _advance_snapshot_state(root: Path, state: Any) -> None:
    """Advance a frozen run through analysis/card steps without enrichment."""
    from osm_polygon_website_tag.pipeline.analyze import analyze_results

    steps: tuple[tuple[str, str, Callable[[], object] | None], ...] = (
        (STATUS_EXTRACTING, STATUS_EXTRACTED, None),
        (STATUS_EXTRACTED, STATUS_ENRICHING, None),
        (STATUS_ENRICHING, STATUS_ENRICHED, None),
        (STATUS_ENRICHED, STATUS_ANALYZED, lambda: analyze_results(root)),
        (STATUS_ANALYZED, STATUS_CARD_BUILT, lambda: build_card(root)),
    )
    for expected, next_status, action in steps:
        if state.metadata.get("status") != expected:
            continue
        if action is not None:
            action()
        transition_status(state, next_status)


def _failed_snapshot_report(message: str) -> FinalizationReport:
    return FinalizationReport(False, {}, VerificationReport(False, [message]))


def _unfinished_text_status_errors(root: Path) -> list[str]:
    return [
        error
        for shard in sorted((root / "polygons").glob("*.parquet"))
        if (error := _unfinished_shard_error(shard)) is not None
    ]


def _unfinished_shard_error(shard: Path) -> str | None:
    """Return an error when a shard contains null or pending text status."""
    parquet = pq.ParquetFile(shard)
    for column in ("website_text_status", "contact_website_text_status"):
        if _column_has_unfinished_status(parquet, column):
            return f"{shard.name} contains unfinished text statuses"
    return None


def _column_has_unfinished_status(parquet: pq.ParquetFile, column: str) -> bool:
    """Scan one status column in bounded Arrow batches."""
    for batch in parquet.iter_batches(columns=[column], batch_size=8_192):
        if any(value is None or value == "pending" for value in batch.column(column).to_pylist()):
            return True
    return False


def finalize_run(run_dir: Path | str) -> FinalizationReport:
    """Verify extraction artifacts, complete lifecycle, and write receipt."""
    root = Path(run_dir)
    report = verify_results(root)
    if not report.ok:
        return FinalizationReport(False, {}, report)
    state = load_run(root)
    status = state.metadata.get("status")
    if status == STATUS_CARD_BUILT:
        transition_status(state, STATUS_VERIFIED)
        transition_status(state, STATUS_COMPLETE)
    elif status != STATUS_COMPLETE:
        failed = VerificationReport(
            False,
            [f"finalization requires card_built or complete state, got {status!r}"],
            report.checked_shards,
        )
        return FinalizationReport(False, {}, failed)
    receipt = _write_completion_receipt(root)
    return FinalizationReport(True, receipt, report)


def _write_completion_receipt(root: Path) -> dict[str, Any]:
    artifacts = [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": hash_file(path),
        }
        for path in publishable_paths(root)
    ]
    canonical = json.dumps(artifacts, sort_keys=True, separators=(",", ":"))
    sources = json.loads((root / "manifests" / "sources.json").read_text(encoding="utf-8"))
    receipt = {
        "schema_version": "v1.2",
        "digest_algorithm": "sha256",
        "manifest_digest": hashlib.sha256(canonical.encode()).hexdigest(),
        "sources_count": len(sources),
        "artifacts": artifacts,
    }
    if (root / POLYGON_DENSITY_ASSET_REL_PATH).is_file():
        receipt["card_contract_version"] = 1
    destination = root / "manifests" / "completion_receipt.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return receipt


def replace_receipt_atomic(run_dir: Path | str) -> dict[str, Any]:
    """Write a current content-only receipt after a refresh-specific verification."""
    return _write_completion_receipt(Path(run_dir))
