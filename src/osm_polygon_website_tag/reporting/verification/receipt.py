"""Validation of completion-receipt publication safety."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.reporting.artifact_inventory import (
    data_manifest_sha256,
    hash_file,
    publishable_paths,
)
from osm_polygon_website_tag.reporting.card import (
    CARD_CONTRACT_VERSION,
    readme_preserved_sha256,
    yaml_custom_sha256,
)
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.text_population import text_population_manifest_entries
from osm_polygon_website_tag.runtime.run_state import OPERATIONAL_MANIFEST_NAMES

_REFRESHABLE_CARD_PATHS = frozenset(
    (
        "README.md",
        "dataset.yaml",
        "stats.json",
        POLYGON_DENSITY_ASSET_REL_PATH,
    )
)


def verify_receipt(root: Path, errors: list[str]) -> None:
    """Verify the completion receipt and every artifact it binds."""
    _verify_receipt(root, errors, allow_refreshable_card_metadata=False)


def verify_receipt_before_card_refresh(root: Path, errors: list[str]) -> None:
    """Verify all untouched receipt artifacts before a card refresh."""
    _verify_receipt(root, errors, allow_refreshable_card_metadata=True)


def _verify_receipt(
    root: Path,
    errors: list[str],
    *,
    allow_refreshable_card_metadata: bool,
) -> None:
    """Run the receipt verifier with one fixed release-refresh compatibility mode."""
    path = root / "manifests" / "completion_receipt.json"
    receipt = _read_receipt(path, errors)
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("completion receipt has no artifact list")
        return
    contract_version = receipt.get("card_contract_version")
    if allow_refreshable_card_metadata:
        _verify_card_contract_before_card_refresh(root, contract_version, errors)
    else:
        _verify_card_contract(root, contract_version, errors)
    seen, canonical_entries = _verify_receipt_artifacts(
        root,
        artifacts,
        contract_version,
        errors,
        allow_refreshable_card_metadata,
    )
    _verify_receipt_inventory(root, seen, errors, allow_refreshable_card_metadata)
    _verify_receipt_digest(receipt, canonical_entries, errors)
    _verify_data_manifest(root, receipt, errors)
    _verify_text_population_manifest(root, receipt, errors)
    _verify_yaml_custom_identity(
        root,
        receipt,
        errors,
        allow_refreshable_card_metadata=allow_refreshable_card_metadata,
    )
    _verify_readme_preserved_identity(
        root,
        receipt,
        errors,
        allow_refreshable_card_metadata=allow_refreshable_card_metadata,
    )


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
    if contract_version == CARD_CONTRACT_VERSION:
        _verify_current_card_contract(map_path, errors)
    elif contract_version == 1:
        errors.append("receipt has stale card_contract_version: 1")
    else:
        _verify_legacy_card_contract(map_path, errors)


def _verify_current_card_contract(map_path: Path, errors: list[str]) -> None:
    if not map_path.is_file():
        errors.append(f"missing map artifact: {POLYGON_DENSITY_ASSET_REL_PATH}")
    if not map_path.parent.parent.joinpath("stats.json").is_file():
        errors.append("missing card artifact: stats.json")


def _verify_card_contract_before_card_refresh(
    root: Path,
    contract_version: object,
    errors: list[str],
) -> None:
    """Allow current and recognized legacy cards before replacing all metadata."""
    if contract_version == CARD_CONTRACT_VERSION:
        return
    if contract_version == 1:
        return
    _verify_card_contract(root, contract_version, errors)


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
    allow_refreshable_card_metadata: bool = False,
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
            allow_refreshable_card_metadata,
        )
    return seen, canonical_entries


def _verify_receipt_entry(
    root: Path,
    entry: Any,
    contract_version: object,
    seen: set[str],
    errors: list[str],
    canonical_entries: list[dict[str, Any]],
    allow_refreshable_card_metadata: bool = False,
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
    _verify_receipt_artifact(
        root,
        relative,
        entry,
        errors,
        canonical_entries,
        allow_refreshable_card_metadata,
    )


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
    allow_refreshable_card_metadata: bool = False,
) -> None:
    if allow_refreshable_card_metadata and relative in _REFRESHABLE_CARD_PATHS:
        _append_refreshable_receipt_entry(relative, entry, canonical_entries)
        return
    artifact = root / relative
    if not artifact.is_file():
        errors.append(f"missing receipt-bound artifact: {relative}")
        return
    _verify_receipt_artifact_digest(artifact, relative, entry, errors, canonical_entries)


def _append_refreshable_receipt_entry(
    relative: str,
    entry: dict[str, Any],
    canonical_entries: list[dict[str, Any]],
) -> None:
    """Retain receipt metadata for card files that the release will refresh."""
    canonical_entries.append(
        {
            "path": relative,
            "size_bytes": entry.get("size_bytes"),
            "sha256": entry.get("sha256"),
        }
    )


def _verify_receipt_artifact_digest(
    artifact: Path,
    relative: str,
    entry: dict[str, Any],
    errors: list[str],
    canonical_entries: list[dict[str, Any]],
) -> None:
    """Compare one existing receipt-bound artifact and record its digest."""
    size = artifact.stat().st_size
    digest = hash_file(artifact)
    if entry.get("size_bytes") != size or entry.get("sha256") != digest:
        errors.append(f"receipt-bound artifact mismatch: {relative}")
    canonical_entries.append({"path": relative, "size_bytes": size, "sha256": digest})


def _verify_receipt_inventory(
    root: Path,
    seen: set[str],
    errors: list[str],
    allow_refreshable_card_metadata: bool = False,
) -> None:
    expected_paths = {path.relative_to(root).as_posix() for path in publishable_paths(root)}
    if allow_refreshable_card_metadata:
        seen -= _REFRESHABLE_CARD_PATHS
        expected_paths -= _REFRESHABLE_CARD_PATHS
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


def _verify_data_manifest(root: Path, receipt: dict[str, Any], errors: list[str]) -> None:
    """Ensure the receipt binds the current source and Parquet inventory."""
    identity = receipt.get("data_manifest_sha256")
    if receipt.get("schema_version") == "v1.2" and "data_manifest_sha256" not in receipt:
        return
    if identity != data_manifest_sha256(root):
        errors.append("completion receipt data manifest mismatch")


def _verify_text_population_manifest(
    root: Path,
    receipt: dict[str, Any],
    errors: list[str],
) -> None:
    """Verify the selected text shards, including supported external layouts."""
    expected = receipt.get("text_population_manifest")
    if expected is None:
        return
    try:
        actual = list(text_population_manifest_entries(root))
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"completion receipt text population manifest unreadable: {exc}")
        return
    if expected != actual:
        errors.append("completion receipt text population manifest mismatch")


def _verify_yaml_custom_identity(
    root: Path,
    receipt: dict[str, Any],
    errors: list[str],
    *,
    allow_refreshable_card_metadata: bool = False,
) -> None:
    """Keep non-generated YAML fields receipt-bound across release refreshes."""
    checked = _verify_one_yaml_custom_identity(
        root, receipt, "dataset_yaml_custom_sha256", "dataset.yaml", errors
    )
    checked = (
        _verify_one_yaml_custom_identity(
            root, receipt, "readme_yaml_custom_sha256", "README.md", errors
        )
        or checked
    )
    if checked:
        return
    if allow_refreshable_card_metadata:
        errors.append("completion receipt has no trusted custom YAML identity")
        return
    _verify_legacy_yaml_custom_identity(root, errors)


def _verify_one_yaml_custom_identity(
    root: Path,
    receipt: dict[str, Any],
    field: str,
    relative: str,
    errors: list[str],
) -> bool:
    """Verify one receipt-bound YAML identity and report whether it existed."""
    expected = receipt.get(field)
    if expected is None:
        return False
    unreadable, actual = _read_yaml_custom_identity(root / relative, relative, errors)
    if unreadable:
        return True
    if actual is None:
        _report_missing_yaml_identity(root / relative, relative, errors)
        return True
    if actual != expected:
        errors.append(f"completion receipt custom YAML identity mismatch: {relative}")
    return True


def _report_missing_yaml_identity(path: Path, relative: str, errors: list[str]) -> None:
    """Reject an existing YAML artifact that cannot provide front matter."""
    if path.is_file():
        errors.append(f"completion receipt custom YAML identity mismatch: {relative}")


def _read_yaml_custom_identity(
    path: Path,
    relative: str,
    errors: list[str],
) -> tuple[bool, str | None]:
    """Read one YAML identity and report filesystem or decoding failures."""
    try:
        return False, yaml_custom_sha256(path)
    except (OSError, UnicodeError) as exc:
        errors.append(f"completion receipt custom YAML identity unreadable: {relative}: {exc}")
        return True, None


def _verify_readme_preserved_identity(
    root: Path,
    receipt: dict[str, Any],
    errors: list[str],
    *,
    allow_refreshable_card_metadata: bool,
) -> None:
    """Bind non-generated README body sections across release refreshes."""
    expected = receipt.get("readme_preserved_sha256")
    if expected is None:
        _report_missing_readme_body_identity(root, errors, allow_refreshable_card_metadata)
        return
    if not isinstance(expected, str) or not expected:
        errors.append("completion receipt has invalid README body identity")
        return
    _verify_existing_readme_body_identity(root, expected, errors, allow_refreshable_card_metadata)


def _report_missing_readme_body_identity(
    root: Path,
    errors: list[str],
    allow_refreshable_card_metadata: bool,
) -> None:
    """Reject refresh of an existing README without receipt-bound body data."""
    if allow_refreshable_card_metadata and (root / "README.md").is_file():
        errors.append("completion receipt has no trusted README body identity")


def _verify_existing_readme_body_identity(
    root: Path,
    expected: str,
    errors: list[str],
    allow_refreshable_card_metadata: bool,
) -> None:
    """Compare the preserved body of a front-matter README to its receipt."""
    actual = readme_preserved_sha256(root / "README.md")
    if yaml_custom_sha256(root / "README.md") is None:
        return
    if actual is None:
        if not allow_refreshable_card_metadata:
            errors.append("missing receipt-bound artifact: README.md")
    elif actual != expected:
        errors.append("completion receipt README body identity mismatch")


def _verify_legacy_yaml_custom_identity(root: Path, errors: list[str]) -> None:
    """Cross-check legacy YAML documents that lack the stronger receipt fields."""
    try:
        dataset = yaml_custom_sha256(root / "dataset.yaml")
        readme = yaml_custom_sha256(root / "README.md")
    except (OSError, UnicodeError) as exc:
        errors.append(f"completion receipt custom YAML identity unreadable: {exc}")
        return
    if dataset is not None and readme is not None and dataset != readme:
        errors.append("completion receipt custom YAML identity mismatch")
