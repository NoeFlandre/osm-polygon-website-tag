"""Finalize a verified run and bind every publishable artifact."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.reporting.verify import VerificationReport, verify_results
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
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


def _publishable_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory in (
        "polygons",
        "analysis_observations",
        "rejections",
        "analysis",
        "manifests",
    ):
        for path in sorted((root / directory).glob("*")):
            if path.is_file() and path.name != "completion_receipt.json":
                paths.append(path)
    for name in ("README.md", "dataset.yaml", "failures.jsonl"):
        path = root / name
        if path.is_file():
            paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _write_completion_receipt(root: Path) -> dict[str, Any]:
    artifacts = [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _hash_file(path),
        }
        for path in _publishable_paths(root)
    ]
    canonical = json.dumps(artifacts, sort_keys=True, separators=(",", ":"))
    sources = json.loads((root / "manifests" / "sources.json").read_text())
    receipt = {
        "schema_version": "v1.2",
        "digest_algorithm": "sha256",
        "manifest_digest": hashlib.sha256(canonical.encode()).hexdigest(),
        "sources_count": len(sources),
        "artifacts": artifacts,
    }
    destination = root / "manifests" / "completion_receipt.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    temporary.replace(destination)
    return receipt


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
