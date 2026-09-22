"""Focused contracts for private completion-receipt verification helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.verification import receipt


def test_receipt_helpers_validate_paths_files_and_digests(tmp_path: Path) -> None:
    assert receipt._unsafe_receipt_path("/absolute")
    assert receipt._unsafe_receipt_path("../escape")
    assert not receipt._unsafe_receipt_path("polygons/a.parquet")
    errors: list[str] = []
    receipt._verify_receipt_entry_metadata("manifests/completion_receipt.json", 1, errors)
    assert errors == [
        "current receipt contains operational artifact: manifests/completion_receipt.json"
    ]
    errors.clear()
    artifact = tmp_path / "README.md"
    artifact.write_text("hello", encoding="utf-8")
    canonical: list[dict[str, object]] = []
    receipt._verify_receipt_artifact(
        tmp_path,
        "README.md",
        {
            "size_bytes": artifact.stat().st_size,
            "sha256": hashlib.sha256(b"hello").hexdigest(),
        },
        errors,
        canonical,
    )
    assert errors == []
    assert canonical[0]["path"] == "README.md"
    errors.clear()
    receipt._verify_receipt_artifact(
        tmp_path,
        "README.md",
        {"size_bytes": 999, "sha256": "0" * 64},
        errors,
        [],
    )
    assert errors == ["receipt-bound artifact mismatch: README.md"]
    errors.clear()
    receipt._verify_receipt_artifact(
        tmp_path,
        "missing.txt",
        {},
        errors,
        [],
    )
    assert errors == ["missing receipt-bound artifact: missing.txt"]
    errors.clear()
    receipt._verify_receipt_digest(
        {
            "manifest_digest": hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        },
        canonical,
        errors,
    )
    errors.clear()
    receipt._verify_receipt_digest(
        {"manifest_digest": "wrong"},
        canonical,
        errors,
    )
    assert errors == ["completion receipt digest mismatch"]
    assert receipt._read_receipt(tmp_path / "missing.json", errors) == {}
    receipt._verify_current_card_contract(tmp_path / "map.png", errors)
    receipt._verify_legacy_card_contract(tmp_path / "map.png", errors)
    assert any("missing map artifact" in error for error in errors)
    map_path = tmp_path / "assets" / "map.png"
    map_path.parent.mkdir()
    map_path.write_bytes(b"map")
    (tmp_path / "stats.json").write_text("stats")
    errors.clear()
    receipt._verify_current_card_contract(map_path, errors)
    assert errors == []
    receipt._verify_legacy_card_contract(map_path, errors)
    assert errors == ["receipt missing card_contract_version while map exists"]

    errors.clear()
    receipt._verify_receipt_entry_metadata("manifests/sources.json", 0, errors)
    assert errors == []
    receipt._verify_receipt_entry_metadata("manifests/sources.json", 1, errors)
    assert errors == []

    errors.clear()
    seen: set[str] = set()
    entries: list[dict[str, object]] = []
    receipt._verify_receipt_entry(
        tmp_path,
        {"path": "../escape"},
        1,
        seen,
        errors,
        entries,
    )
    assert errors == ["unsafe completion receipt path"]
    errors.clear()
    receipt._verify_receipt_entry(
        tmp_path,
        {"path": "missing.txt"},
        1,
        seen,
        errors,
        entries,
    )
    assert errors == ["missing receipt-bound artifact: missing.txt"]
    errors.clear()
    receipt._verify_receipt_entry(
        tmp_path,
        {"path": "missing.txt"},
        1,
        seen,
        errors,
        entries,
    )
    assert errors == ["duplicate completion receipt path: missing.txt"]


def test_legacy_yaml_custom_identity_covers_compatibility_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, str | None] = {"dataset.yaml": "same", "README.md": "same"}
    monkeypatch.setattr(receipt, "yaml_custom_sha256", lambda path: values[path.name])
    errors: list[str] = []

    receipt._verify_legacy_yaml_custom_identity(tmp_path, errors)
    assert errors == []

    values["README.md"] = "different"
    receipt._verify_legacy_yaml_custom_identity(tmp_path, errors)
    assert errors == ["completion receipt custom YAML identity mismatch"]

    errors.clear()
    values["README.md"] = None
    receipt._verify_legacy_yaml_custom_identity(tmp_path, errors)
    assert errors == []

    def unreadable(_path: Path) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(receipt, "yaml_custom_sha256", unreadable)
    receipt._verify_legacy_yaml_custom_identity(tmp_path, errors)
    assert errors == ["completion receipt custom YAML identity unreadable: unreadable"]


def test_readme_body_identity_is_strict_and_refresh_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "README.md").write_text("# card\n", encoding="utf-8")
    errors: list[str] = []

    receipt._report_missing_readme_body_identity(tmp_path, errors, False)
    assert errors == []
    receipt._report_missing_readme_body_identity(tmp_path, errors, True)
    assert errors == ["completion receipt has no trusted README body identity"]

    monkeypatch.setattr(receipt, "yaml_custom_sha256", lambda _path: "custom")
    monkeypatch.setattr(receipt, "readme_preserved_sha256", lambda _path: None)
    errors.clear()
    receipt._verify_existing_readme_body_identity(tmp_path, "expected", errors, True)
    assert errors == []

    monkeypatch.setattr(receipt, "readme_preserved_sha256", lambda _path: "actual")
    receipt._verify_existing_readme_body_identity(tmp_path, "expected", errors, False)
    assert errors == ["completion receipt README body identity mismatch"]


def test_readme_identity_rejects_non_string_receipt_values(tmp_path: Path) -> None:
    errors: list[str] = []

    receipt._verify_readme_preserved_identity(
        tmp_path,
        {"readme_preserved_sha256": 123},
        errors,
        allow_refreshable_card_metadata=False,
    )

    assert errors == ["completion receipt has invalid README body identity"]


def test_readme_identity_forwards_refresh_policy_and_uses_exact_readme_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, Path]] = []

    def readme_hash(path: Path) -> str:
        observed.append(("readme", path))
        return "expected"

    def yaml_hash(path: Path) -> str:
        observed.append(("yaml", path))
        return "custom"

    monkeypatch.setattr(receipt, "readme_preserved_sha256", readme_hash)
    monkeypatch.setattr(receipt, "yaml_custom_sha256", yaml_hash)
    errors: list[str] = []

    receipt._verify_existing_readme_body_identity(tmp_path, "expected", errors, False)

    assert errors == []
    assert observed == [
        ("readme", tmp_path / "README.md"),
        ("yaml", tmp_path / "README.md"),
    ]

    observed.clear()
    monkeypatch.setattr(receipt, "readme_preserved_sha256", lambda _path: None)
    receipt._verify_existing_readme_body_identity(tmp_path, "expected", errors, True)
    assert errors == []

    forwarded: list[bool | None] = []
    monkeypatch.setattr(
        receipt,
        "_verify_existing_readme_body_identity",
        lambda _root, _expected, _errors, allow: forwarded.append(allow),
    )
    receipt._verify_readme_preserved_identity(
        tmp_path,
        {"readme_preserved_sha256": "expected"},
        errors,
        allow_refreshable_card_metadata=True,
    )
    assert forwarded == [True]


def test_missing_readme_identity_requires_the_canonical_readme_path(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# card\n", encoding="utf-8")
    errors: list[str] = []

    receipt._report_missing_readme_body_identity(tmp_path, errors, True)

    assert errors == ["completion receipt has no trusted README body identity"]


def test_missing_readme_identity_requests_the_canonical_filename() -> None:
    requested: list[str] = []

    class Readme:
        def is_file(self) -> bool:
            return True

    class Root:
        def __truediv__(self, name: str) -> Readme:
            requested.append(name)
            return Readme()

    errors: list[str] = []
    receipt._report_missing_readme_body_identity(cast(Path, Root()), errors, True)

    assert requested == ["README.md"]
    assert errors == ["completion receipt has no trusted README body identity"]


def test_current_card_contract_requires_lowercase_stats_json(tmp_path: Path) -> None:
    map_path = tmp_path / POLYGON_DENSITY_ASSET_REL_PATH
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"png")
    (tmp_path / "stats.json").write_bytes(b"{}\n")
    errors: list[str] = []

    requested: list[Path] = []
    real_is_file = Path.is_file

    def is_file(path: Path) -> bool:
        requested.append(path)
        return real_is_file(path)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "is_file", is_file)
    try:
        receipt._verify_current_card_contract(map_path, errors)
    finally:
        monkeypatch.undo()

    assert errors == []
    assert tmp_path / "stats.json" in requested


def test_yaml_custom_identity_reports_missing_and_unreadable_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = tmp_path / "dataset.yaml"
    dataset.write_text("license: odbl\n", encoding="utf-8")
    errors: list[str] = []
    monkeypatch.setattr(receipt, "yaml_custom_sha256", lambda _path: None)

    assert receipt._verify_one_yaml_custom_identity(
        tmp_path,
        {"dataset_yaml_custom_sha256": "expected"},
        "dataset_yaml_custom_sha256",
        "dataset.yaml",
        errors,
    )
    assert errors == ["completion receipt custom YAML identity mismatch: dataset.yaml"]

    received: list[tuple[Path, str, list[str]]] = []
    real_read_identity = receipt._read_yaml_custom_identity

    def read_identity(path: Path, relative: str, received_errors: list[str]) -> tuple[bool, str]:
        received.append((path, relative, received_errors))
        return False, "expected"

    monkeypatch.setattr(receipt, "_read_yaml_custom_identity", read_identity)
    errors.clear()
    assert receipt._verify_one_yaml_custom_identity(
        tmp_path,
        {"dataset_yaml_custom_sha256": "expected"},
        "dataset_yaml_custom_sha256",
        "dataset.yaml",
        errors,
    )
    assert received == [(dataset, "dataset.yaml", errors)]

    def unreadable(_path: Path) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(receipt, "yaml_custom_sha256", unreadable)
    monkeypatch.setattr(receipt, "_read_yaml_custom_identity", real_read_identity)
    errors.clear()
    assert receipt._read_yaml_custom_identity(dataset, "dataset.yaml", errors) == (True, None)
    assert errors == [
        "completion receipt custom YAML identity unreadable: dataset.yaml: unreadable"
    ]


def test_yaml_custom_identity_keeps_legacy_and_refresh_policies_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[str] = []
    legacy_calls: list[Path] = []
    real_verify_one = receipt._verify_one_yaml_custom_identity
    real_verify_legacy = receipt._verify_legacy_yaml_custom_identity
    monkeypatch.setattr(
        receipt,
        "_verify_one_yaml_custom_identity",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        receipt,
        "_verify_legacy_yaml_custom_identity",
        lambda root, _errors: legacy_calls.append(root),
    )

    receipt._verify_yaml_custom_identity(tmp_path, {}, errors)
    assert legacy_calls == [tmp_path]
    receipt._verify_yaml_custom_identity(tmp_path, {}, errors, allow_refreshable_card_metadata=True)
    assert errors == ["completion receipt has no trusted custom YAML identity"]

    monkeypatch.setattr(receipt, "_verify_one_yaml_custom_identity", real_verify_one)
    monkeypatch.setattr(receipt, "_verify_legacy_yaml_custom_identity", real_verify_legacy)
    monkeypatch.setattr(
        receipt,
        "yaml_custom_sha256",
        lambda path: "dataset" if path.name == "dataset.yaml" else "readme",
    )
    errors.clear()
    receipt._verify_yaml_custom_identity(tmp_path, {}, errors)
    assert errors == ["completion receipt custom YAML identity mismatch"]


def test_text_population_manifest_binds_expected_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(receipt, "text_population_manifest_entries", lambda _root: ["one"])
    errors: list[str] = []

    receipt._verify_text_population_manifest(
        tmp_path,
        {"text_population_manifest": ["one"]},
        errors,
    )
    assert errors == []

    receipt._verify_text_population_manifest(
        tmp_path,
        {"text_population_manifest": ["two"]},
        errors,
    )
    assert errors == ["completion receipt text population manifest mismatch"]

    def unreadable(_root: Path) -> list[str]:
        raise OSError("unreadable")

    monkeypatch.setattr(receipt, "text_population_manifest_entries", unreadable)
    errors.clear()
    receipt._verify_text_population_manifest(
        tmp_path,
        {"text_population_manifest": ["one"]},
        errors,
    )
    assert errors == ["completion receipt text population manifest unreadable: unreadable"]


def test_yaml_custom_identity_dispatches_both_receipt_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str, list[str] | None]] = []
    legacy_calls: list[Path] = []

    def verify_one(
        _root: Path,
        _receipt: dict[str, object],
        field: str | None,
        relative: str,
        errors: list[str] | None,
    ) -> bool:
        calls.append((field, relative, errors))
        return field == "readme_yaml_custom_sha256"

    monkeypatch.setattr(receipt, "_verify_one_yaml_custom_identity", verify_one)
    monkeypatch.setattr(
        receipt,
        "_verify_legacy_yaml_custom_identity",
        lambda root, _errors: legacy_calls.append(root),
    )
    errors: list[str] = []

    receipt._verify_yaml_custom_identity(
        tmp_path,
        {
            "dataset_yaml_custom_sha256": "dataset",
            "readme_yaml_custom_sha256": "readme",
        },
        errors,
    )

    assert calls == [
        ("dataset_yaml_custom_sha256", "dataset.yaml", errors),
        ("readme_yaml_custom_sha256", "README.md", errors),
    ]
    assert legacy_calls == []


def test_receipt_entrypoints_and_orchestrator_forward_strict_and_refresh_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[str] = []
    calls: list[tuple[str, object]] = []
    entry = {"path": "README.md"}

    monkeypatch.setattr(
        receipt,
        "_read_receipt",
        lambda path, received_errors: (
            calls.append(("read", (path, received_errors)))
            or {"artifacts": [entry], "card_contract_version": 2}
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_card_contract",
        lambda root, version, received_errors: calls.append(
            ("card", (root, version, received_errors))
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_card_contract_before_card_refresh",
        lambda root, version, received_errors: calls.append(
            ("refresh-card", (root, version, received_errors))
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_receipt_artifacts",
        lambda root, artifacts, version, received_errors, allow: (
            calls.append(("artifacts", (root, artifacts, version, received_errors, allow)))
            or ({"README.md"}, [{"path": "README.md", "size_bytes": 1, "sha256": "a"}])
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_receipt_inventory",
        lambda root, seen, received_errors, allow: calls.append(
            ("inventory", (root, seen, received_errors, allow))
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_receipt_digest",
        lambda payload, canonical, received_errors: calls.append(
            ("digest", (payload, canonical, received_errors))
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_data_manifest",
        lambda root, payload, received_errors: calls.append(
            ("data", (root, payload, received_errors))
        ),
    )
    policy_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        receipt,
        "_verify_yaml_custom_identity",
        lambda _root, _payload, _errors, *, allow_refreshable_card_metadata: policy_calls.append(
            ("yaml", allow_refreshable_card_metadata)
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_readme_preserved_identity",
        lambda _root, _payload, _errors, *, allow_refreshable_card_metadata: policy_calls.append(
            ("readme", allow_refreshable_card_metadata)
        ),
    )

    receipt._verify_receipt(tmp_path, errors, allow_refreshable_card_metadata=False)
    assert calls == [
        ("read", (tmp_path / "manifests" / "completion_receipt.json", errors)),
        ("card", (tmp_path, 2, errors)),
        ("artifacts", (tmp_path, [entry], 2, errors, False)),
        ("inventory", (tmp_path, {"README.md"}, errors, False)),
        (
            "digest",
            (
                {"artifacts": [entry], "card_contract_version": 2},
                [{"path": "README.md", "size_bytes": 1, "sha256": "a"}],
                errors,
            ),
        ),
        (
            "data",
            (tmp_path, {"artifacts": [entry], "card_contract_version": 2}, errors),
        ),
    ]
    assert policy_calls == [("yaml", False), ("readme", False)]

    calls.clear()
    policy_calls.clear()
    receipt._verify_receipt(tmp_path, errors, allow_refreshable_card_metadata=True)
    assert calls[1:] == [
        ("refresh-card", (tmp_path, 2, errors)),
        ("artifacts", (tmp_path, [entry], 2, errors, True)),
        ("inventory", (tmp_path, {"README.md"}, errors, True)),
        (
            "digest",
            (
                {"artifacts": [entry], "card_contract_version": 2},
                [{"path": "README.md", "size_bytes": 1, "sha256": "a"}],
                errors,
            ),
        ),
        (
            "data",
            (tmp_path, {"artifacts": [entry], "card_contract_version": 2}, errors),
        ),
    ]
    assert policy_calls == [("yaml", True), ("readme", True)]

    calls.clear()
    monkeypatch.setattr(receipt, "_read_receipt", lambda *_args: {})
    errors.clear()
    receipt._verify_receipt(tmp_path, errors, allow_refreshable_card_metadata=False)
    assert errors == ["completion receipt has no artifact list"]
    assert calls == []

    calls.clear()
    receipt.verify_receipt(tmp_path, errors)
    receipt.verify_receipt_before_card_refresh(tmp_path, errors)
    assert calls == []  # the wrappers are checked by the focused spy below


def test_receipt_public_wrappers_pass_their_fixed_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, list[str], bool]] = []
    monkeypatch.setattr(
        receipt,
        "_verify_receipt",
        lambda root, errors, *, allow_refreshable_card_metadata: calls.append(
            (root, errors, allow_refreshable_card_metadata)
        ),
    )
    errors: list[str] = []
    receipt.verify_receipt(tmp_path, errors)
    receipt.verify_receipt_before_card_refresh(tmp_path, errors)
    assert calls == [(tmp_path, errors, False), (tmp_path, errors, True)]


def test_receipt_contract_versions_and_current_artifacts_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    real_current_card_contract = receipt._verify_current_card_contract
    monkeypatch.setattr(
        receipt,
        "_verify_current_card_contract",
        lambda path, errors: calls.append(("current", (path, errors))),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_legacy_card_contract",
        lambda path, errors: calls.append(("legacy", (path, errors))),
    )
    errors: list[str] = []
    receipt._verify_card_contract(tmp_path, receipt.CARD_CONTRACT_VERSION, errors)
    assert calls == [("current", (tmp_path / POLYGON_DENSITY_ASSET_REL_PATH, errors))]
    calls.clear()
    receipt._verify_card_contract(tmp_path, 1, errors)
    assert errors == ["receipt has stale card_contract_version: 1"]
    calls.clear()
    errors.clear()
    receipt._verify_card_contract(tmp_path, 0, errors)
    assert calls == [("legacy", (tmp_path / POLYGON_DENSITY_ASSET_REL_PATH, errors))]

    map_path = tmp_path / POLYGON_DENSITY_ASSET_REL_PATH
    errors.clear()
    real_current_card_contract(map_path, errors)
    assert errors == [
        f"missing map artifact: {POLYGON_DENSITY_ASSET_REL_PATH}",
        "missing card artifact: stats.json",
    ]
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"png")
    errors.clear()
    real_current_card_contract(map_path, errors)
    assert errors == ["missing card artifact: stats.json"]
    (tmp_path / "stats.json").write_text("{}", encoding="utf-8")
    errors.clear()
    real_current_card_contract(map_path, errors)
    assert errors == []

    errors.clear()
    receipt._verify_card_contract_before_card_refresh(tmp_path, 2, errors)
    assert errors == []
    map_path.unlink()
    receipt._verify_card_contract_before_card_refresh(tmp_path, 2, errors)
    assert errors == []

    delegated: list[tuple[Path, object, list[str]]] = []
    monkeypatch.setattr(
        receipt,
        "_verify_card_contract",
        lambda root, version, received_errors: delegated.append((root, version, received_errors)),
    )
    errors.clear()
    receipt._verify_card_contract_before_card_refresh(tmp_path, 1, errors)
    assert delegated == []
    assert errors == []


def test_legacy_card_contract_reports_both_missing_version_cases(tmp_path: Path) -> None:
    errors: list[str] = []
    receipt._verify_legacy_card_contract(tmp_path / POLYGON_DENSITY_ASSET_REL_PATH, errors)
    assert errors == ["receipt missing card_contract_version for current publication"]
    map_path = tmp_path / POLYGON_DENSITY_ASSET_REL_PATH
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"png")
    errors.clear()
    receipt._verify_legacy_card_contract(map_path, errors)
    assert errors == ["receipt missing card_contract_version while map exists"]


def test_receipt_read_and_entry_helpers_forward_valid_entries_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "receipt.json"
    path.write_text('{"artifacts": []}', encoding="utf-8")
    errors: list[str] = []
    assert receipt._read_receipt(path, errors) == {"artifacts": []}
    path.write_text("[]", encoding="utf-8")
    receipt._read_receipt(path, errors)
    assert errors[-1] == f"expected JSON object: {path}"

    metadata_calls: list[tuple[str, object, list[str]]] = []
    artifact_calls: list[tuple[str, object, list[str], list[dict[str, object]], bool]] = []
    monkeypatch.setattr(
        receipt,
        "_verify_receipt_entry_metadata",
        lambda relative, version, received_errors: metadata_calls.append(
            (relative, version, received_errors)
        ),
    )
    monkeypatch.setattr(
        receipt,
        "_verify_receipt_artifact",
        lambda root, relative, entry, received_errors, canonical, allow: artifact_calls.append(
            (relative, entry, received_errors, canonical, allow)
        ),
    )
    seen: set[str] = set()
    canonical: list[dict[str, object]] = []
    entry = {"path": "README.md", "size_bytes": 1, "sha256": "a"}
    receipt._verify_receipt_entry(tmp_path, entry, 2, seen, errors, canonical)
    assert seen == {"README.md"}
    assert metadata_calls == [("README.md", 2, errors)]
    assert artifact_calls == [("README.md", entry, errors, canonical, False)]

    errors.clear()
    receipt._verify_receipt_entry(tmp_path, "not-a-dict", 2, set(), errors, [])
    assert errors == ["invalid completion receipt artifact entry"]


def test_read_receipt_requires_explicit_utf8_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def read_text(_path: Path, *, encoding: object) -> str:
        calls.append(encoding)
        return '{"artifacts": []}'

    monkeypatch.setattr(Path, "read_text", read_text)
    errors: list[str] = []

    assert receipt._read_receipt(tmp_path / "receipt.json", errors) == {"artifacts": []}
    assert calls == ["utf-8"]


def test_receipt_artifact_helpers_cover_refreshable_and_independent_digest_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    canonical: list[dict[str, object]] = []
    errors: list[str] = []
    monkeypatch.setattr(
        receipt,
        "_append_refreshable_receipt_entry",
        lambda relative, entry, received: calls.append(("refresh", (relative, entry, received))),
    )
    receipt._verify_receipt_artifact(
        tmp_path,
        "README.md",
        {"size_bytes": 1, "sha256": "a"},
        errors,
        canonical,
        allow_refreshable_card_metadata=True,
    )
    assert calls == [("refresh", ("README.md", {"size_bytes": 1, "sha256": "a"}, canonical))]

    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"hello")
    expected_digest = hashlib.sha256(b"hello").hexdigest()
    errors.clear()
    canonical.clear()
    receipt._verify_receipt_artifact_digest(
        artifact,
        "artifact",
        {"size_bytes": 6, "sha256": expected_digest},
        errors,
        canonical,
    )
    assert errors == ["receipt-bound artifact mismatch: artifact"]
    assert canonical == [{"path": "artifact", "size_bytes": 5, "sha256": expected_digest}]
    errors.clear()
    canonical.clear()
    receipt._verify_receipt_artifact_digest(
        artifact,
        "artifact",
        {"size_bytes": 5, "sha256": "wrong"},
        errors,
        canonical,
    )
    assert errors == ["receipt-bound artifact mismatch: artifact"]


def test_receipt_artifact_collection_and_canonical_entry_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[
        tuple[Path, object, object, set[str], list[str], list[dict[str, object]], bool]
    ] = []

    def verify_entry(
        root: Path,
        entry: object,
        version: object,
        seen: set[str],
        errors: list[str],
        canonical: list[dict[str, object]],
        allow: bool,
    ) -> None:
        calls.append((root, entry, version, seen, errors, canonical, allow))
        seen.add(str(entry))

    monkeypatch.setattr(receipt, "_verify_receipt_entry", verify_entry)
    errors: list[str] = []
    seen, canonical = receipt._verify_receipt_artifacts(tmp_path, ["one", "two"], 2, errors)
    assert seen == {"one", "two"}
    assert canonical == []
    assert [call[2] for call in calls] == [2, 2]
    assert [call[-1] for call in calls] == [False, False]
    calls.clear()
    receipt._verify_receipt_artifacts(tmp_path, ["one"], 2, errors, True)
    assert calls[-1][-1] is True

    refresh_entry: dict[str, object] = {"path": "README.md", "size_bytes": 5, "sha256": "a"}
    target: list[dict[str, object]] = []
    receipt._append_refreshable_receipt_entry("README.md", refresh_entry, target)
    assert target == [{"path": "README.md", "size_bytes": 5, "sha256": "a"}]


def test_receipt_inventory_digest_and_data_identity_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "polygons").mkdir()
    (tmp_path / "polygons" / "a.parquet").write_bytes(b"data")
    (tmp_path / "README.md").write_bytes(b"readme")
    (tmp_path / "stats.json").write_bytes(b"stats")
    expected_paths = {"README.md", "stats.json", "polygons/a.parquet"}
    errors: list[str] = []
    receipt._verify_receipt_inventory(tmp_path, set(expected_paths), errors)
    assert errors == []
    receipt._verify_receipt_inventory(tmp_path, expected_paths - {"stats.json"}, errors)
    assert errors == ["completion receipt artifact inventory mismatch"]

    errors.clear()
    receipt._verify_receipt_inventory(tmp_path, set(expected_paths), errors, True)
    assert errors == []
    errors.clear()
    receipt._verify_receipt_inventory(tmp_path, set(), errors, True)
    assert errors == ["completion receipt artifact inventory mismatch"]

    entries = [
        {"path": "z", "size_bytes": 1, "sha256": "z"},
        {"path": "a", "size_bytes": 2, "sha256": "a"},
    ]
    canonical = json.dumps(
        sorted(entries, key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    errors.clear()
    receipt._verify_receipt_digest(
        {"manifest_digest": hashlib.sha256(canonical.encode()).hexdigest()},
        entries,
        errors,
    )
    assert errors == []

    manifest_calls: list[Path] = []
    monkeypatch.setattr(
        receipt,
        "data_manifest_sha256",
        lambda root: manifest_calls.append(root) or "digest",
    )
    errors.clear()
    receipt._verify_data_manifest(tmp_path, {"data_manifest_sha256": "digest"}, errors)
    assert errors == []
    assert manifest_calls == [tmp_path]
    receipt._verify_data_manifest(tmp_path, {"data_manifest_sha256": "wrong"}, errors)
    assert errors == ["completion receipt data manifest mismatch"]
    errors.clear()
    receipt._verify_data_manifest(
        tmp_path,
        {"schema_version": "v1.2", "data_manifest_sha256": "wrong"},
        errors,
    )
    assert errors == ["completion receipt data manifest mismatch"]
