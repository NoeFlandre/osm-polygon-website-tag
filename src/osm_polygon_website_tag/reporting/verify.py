"""Strict, bounded verification of extraction artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.reporting.verification.analysis import (
    verify_analysis_and_card as _verify_analysis_and_card,
)
from osm_polygon_website_tag.reporting.verification.language import (
    verify_language_invariants as _verify_language_invariants,
)
from osm_polygon_website_tag.reporting.verification.receipt import verify_receipt as _verify_receipt
from osm_polygon_website_tag.reporting.verification.rows import (
    verify_row_invariants as _verify_row_invariants,
)
from osm_polygon_website_tag.reporting.verification.shards import verify_shards as _verify_shards
from osm_polygon_website_tag.reporting.verification.text import (
    verify_text_invariants as _verify_text_invariants,
)
from osm_polygon_website_tag.runtime.run_state import SourceManifestEntry


@dataclass
class VerificationReport:
    """Result of :func:`verify_results`."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    checked_shards: list[str] = field(default_factory=list)


def verify_results(run_dir: Path | str) -> VerificationReport:
    """Verify exact schemas, counts, hashes, inventory, and row invariants."""
    return _verify_results(Path(run_dir), include_receipt=True)


def verify_results_modern(run_dir: Path | str) -> VerificationReport:
    """Verify a newly built card while an older completion receipt is stale."""
    return _verify_results(Path(run_dir), include_receipt=False)


def _verify_results(root: Path, *, include_receipt: bool) -> VerificationReport:
    errors: list[str] = []
    checked: list[str] = []
    metadata = _read_json_object(root / "manifests" / "run.json", errors)
    manifest = _read_source_manifest(root / "manifests" / "sources.json", errors)
    if not manifest:
        errors.append("sources manifest is empty")

    _verify_shards(root, manifest, errors, checked)
    _verify_expected_inventory(root, manifest, errors)
    _verify_row_invariants(root, errors)
    if not metadata:
        errors.append("run metadata is empty")
    status = metadata.get("status")
    _verify_text_invariants(root, status, errors)
    _verify_language_invariants(root, errors)
    _verify_status_artifacts(root, status, include_receipt, errors)
    return VerificationReport(not errors, errors, checked)


def _verify_status_artifacts(
    root: Path,
    status: object,
    include_receipt: bool,
    errors: list[str],
) -> None:
    if status in {"card_built", "verified", "complete"}:
        _verify_analysis_and_card(root, errors)
    if status == "complete" and include_receipt:
        _verify_receipt(root, errors)


def _verify_expected_inventory(
    root: Path,
    manifest: list[SourceManifestEntry],
    errors: list[str],
) -> None:
    path = root / "manifests" / "expected_sources.json"
    if not path.exists():
        return
    expected = _read_source_manifest(path, errors)

    def identity(entry: SourceManifestEntry) -> tuple[str | int | None, ...]:
        return (
            entry.get("filename"),
            entry.get("size_bytes"),
            entry.get("mtime_ns"),
        )

    if sorted(map(identity, expected)) != sorted(map(identity, manifest)):
        errors.append("processed sources do not exactly match expected source inventory")


def _read_source_manifest(path: Path, errors: list[str]) -> list[SourceManifestEntry]:
    """Read a structurally valid JSON array at the typed manifest boundary."""
    return _read_json_array(path, errors)


def _read_json_value(
    path: Path,
    errors: list[str],
    *,
    label: str,
) -> tuple[bool, Any]:
    """Read one JSON value and report parse or encoding failures."""
    try:
        return True, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON {label} {path}: {exc}")
        return False, None


def _read_json_object(path: Path, errors: list[str]) -> dict[str, Any]:
    ok, value = _read_json_value(path, errors, label="object")
    if not ok:
        return {}
    if not isinstance(value, dict):
        errors.append(f"expected JSON object: {path}")
        return {}
    return value


def _read_json_array(path: Path, errors: list[str]) -> list[SourceManifestEntry]:
    ok, value = _read_json_value(path, errors, label="array")
    if not ok:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        errors.append(f"expected array of objects: {path}")
        return []
    return value
