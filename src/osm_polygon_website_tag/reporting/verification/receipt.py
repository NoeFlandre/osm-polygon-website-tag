"""Validation of completion-receipt publication safety."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.reporting.artifact_inventory import hash_file, publishable_paths
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.runtime.run_state import OPERATIONAL_MANIFEST_NAMES


def verify_receipt(root: Path, errors: list[str]) -> None:
    """Verify the completion receipt and every artifact it binds."""
    path = root / "manifests" / "completion_receipt.json"
    receipt = _read_receipt(path, errors)
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("completion receipt has no artifact list")
        return
    contract_version = receipt.get("card_contract_version")
    _verify_card_contract(root, contract_version, errors)
    seen, canonical_entries = _verify_receipt_artifacts(
        root,
        artifacts,
        contract_version,
        errors,
    )
    _verify_receipt_inventory(root, seen, errors)
    _verify_receipt_digest(receipt, canonical_entries, errors)


def _read_receipt(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON object {path}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"expected JSON object: {path}")
        return {}
    return value


def _verify_card_contract(root: Path, contract_version: object, errors: list[str]) -> None:
    map_path = root / POLYGON_DENSITY_ASSET_REL_PATH
    if contract_version == 1:
        _verify_current_card_contract(map_path, errors)
    else:
        _verify_legacy_card_contract(map_path, errors)


def _verify_current_card_contract(map_path: Path, errors: list[str]) -> None:
    if not map_path.is_file():
        errors.append(f"missing map artifact: {POLYGON_DENSITY_ASSET_REL_PATH}")


def _verify_legacy_card_contract(map_path: Path, errors: list[str]) -> None:
    if map_path.is_file():
        errors.append("receipt missing card_contract_version while map exists")
    else:
        errors.append("receipt missing card_contract_version for current publication")


def _verify_receipt_artifacts(
    root: Path,
    artifacts: list[Any],
    contract_version: object,
    errors: list[str],
) -> tuple[set[str], list[dict[str, Any]]]:
    seen: set[str] = set()
    canonical_entries: list[dict[str, Any]] = []
    for entry in artifacts:
        _verify_receipt_entry(
            root,
            entry,
            contract_version,
            seen,
            errors,
            canonical_entries,
        )
    return seen, canonical_entries


def _verify_receipt_entry(
    root: Path,
    entry: Any,
    contract_version: object,
    seen: set[str],
    errors: list[str],
    canonical_entries: list[dict[str, Any]],
) -> None:
    if not isinstance(entry, dict):
        errors.append("invalid completion receipt artifact entry")
        return
    relative = entry.get("path")
    if _unsafe_receipt_path(relative):
        errors.append("unsafe completion receipt path")
        return
    if relative in seen:
        errors.append(f"duplicate completion receipt path: {relative}")
        return
    seen.add(relative)
    _verify_receipt_entry_metadata(relative, contract_version, errors)
    _verify_receipt_artifact(root, relative, entry, errors, canonical_entries)


def _verify_receipt_entry_metadata(
    relative: str,
    contract_version: object,
    errors: list[str],
) -> None:
    if contract_version == 1 and Path(relative).name in OPERATIONAL_MANIFEST_NAMES:
        errors.append(f"current receipt contains operational artifact: {relative}")


def _unsafe_receipt_path(relative: object) -> bool:
    return not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts


def _verify_receipt_artifact(
    root: Path,
    relative: str,
    entry: dict[str, Any],
    errors: list[str],
    canonical_entries: list[dict[str, Any]],
) -> None:
    artifact = root / relative
    if not artifact.is_file():
        errors.append(f"missing receipt-bound artifact: {relative}")
        return
    size = artifact.stat().st_size
    digest = hash_file(artifact)
    if entry.get("size_bytes") != size or entry.get("sha256") != digest:
        errors.append(f"receipt-bound artifact mismatch: {relative}")
    canonical_entries.append({"path": relative, "size_bytes": size, "sha256": digest})


def _verify_receipt_inventory(root: Path, seen: set[str], errors: list[str]) -> None:
    expected_paths = {path.relative_to(root).as_posix() for path in publishable_paths(root)}
    if seen != expected_paths:
        errors.append("completion receipt artifact inventory mismatch")


def _verify_receipt_digest(
    receipt: dict[str, Any],
    canonical_entries: list[dict[str, Any]],
    errors: list[str],
) -> None:
    canonical = json.dumps(
        sorted(canonical_entries, key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    if receipt.get("manifest_digest") != hashlib.sha256(canonical.encode()).hexdigest():
        errors.append("completion receipt digest mismatch")
