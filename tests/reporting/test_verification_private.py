"""Direct contracts for verification helpers used by the release gate."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geometry_stats import GeometryStats
from osm_polygon_website_tag.reporting.verification import analysis, receipt, rows, shards, text


def test_text_verification_helpers_cover_terminal_and_absent_states() -> None:
    assert text._absent_text_is_consistent(None, None, "absent")
    assert not text._absent_text_is_consistent("x", None, "absent")
    assert text._empty_text_is_consistent("", 0)
    assert not text._empty_text_is_consistent(None, 0)
    errors: list[str] = []
    text._verify_one_text_value(
        tag_value=None,
        text=None,
        word_count=None,
        text_status="absent",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )
    text._verify_one_text_value(
        tag_value="https://example.org",
        text="one two",
        word_count=2,
        text_status="success",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )
    assert errors == []
    text._verify_one_text_value(
        tag_value="https://example.org",
        text=None,
        word_count=None,
        text_status="pending",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )
    assert any("remains pending" in error for error in errors)
    text._verify_text_row(
        {
            "website": "https://example.org",
            "website_text": "one two",
            "website_word_count": 2,
            "website_text_status": "success",
            "contact_website": None,
            "contact_website_text": None,
            "contact_website_word_count": None,
            "contact_website_text_status": "absent",
        },
        "a.parquet",
        False,
        [],
    )


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


def test_analysis_and_row_verification_helpers_are_deterministic(tmp_path: Path) -> None:
    errors: list[str] = []
    expected = {"a", "b"}
    assert analysis._verify_cell_set(
        [{"cell": "a"}, {"cell": "b"}], "observation", expected, errors
    )
    assert not analysis._verify_cell_set([{"cell": "a"}], "canonical", expected, errors)
    assert errors
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "sources.json").write_text(
        json.dumps([{"observation_row_count": 2}]), encoding="utf-8"
    )
    analysis._verify_observation_total(tmp_path, [{"row_count": 1}, {"row_count": 1}], [])
    analysis._verify_canonical_total([{"row_count": 1}], [{"row_count": 2}], errors)
    assert (tmp_path / "manifests" / "expected_sources.json").exists() is False
    analysis._verify_expected_source_inventory(tmp_path, errors)
    assert any("expected source inventory" in error for error in errors)
    con = duckdb.connect(":memory:")
    try:
        rows._verify_row_contract(tmp_path, con, "polygons", "TRUE", "public", errors)
    finally:
        con.close()


def test_shard_helpers_report_metadata_errors_without_network(tmp_path: Path) -> None:
    contract = shards.SHARD_CONTRACTS[0]
    errors: list[str] = []
    checked: list[str] = []
    declared: set[str] = set()
    shards._verify_manifest_entry(
        tmp_path,
        {"filename": "a.osm.pbf", "public_row_count": 0, "public_shard_sha256": "0" * 64},
        errors,
        checked,
        declared,
    )
    assert "a" in declared
    assert any("missing public shard" in error for error in errors)
    errors.clear()
    shards._verify_row_count(1, "a.osm.pbf", contract, {"public_row_count": 0}, errors)
    assert errors
    path = tmp_path / "file"
    path.write_bytes(b"x")
    shards._verify_shard_hash(
        path,
        "a.osm.pbf",
        contract,
        {"public_shard_sha256": hashlib.sha256(b"x").hexdigest()},
        errors,
    )
    assert shards._verify_extra_shards(tmp_path, contract, set(), errors) is None


def test_shard_helpers_cover_valid_and_invalid_inventory_branches(
    tmp_path: Path, monkeypatch
) -> None:
    contract = shards.SHARD_CONTRACTS[0]
    errors: list[str] = []
    checked: list[str] = []
    declared: set[str] = set()
    shards._verify_manifest_entry(
        tmp_path,
        {"filename": "not-a-pbf"},
        errors,
        checked,
        declared,
    )
    assert errors == ["manifest entry has invalid filename"]
    errors.clear()

    shard = tmp_path / contract.directory / "a.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"parquet")
    for other_contract in shards.SHARD_CONTRACTS[1:]:
        other = tmp_path / other_contract.directory / "a.parquet"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"parquet")
    fake_parquet = SimpleNamespace(schema_arrow=object(), metadata=SimpleNamespace(num_rows=2))
    monkeypatch.setattr(shards.pq, "ParquetFile", lambda _path: fake_parquet)
    monkeypatch.setattr(shards, "schema_matches", lambda *_args: True)
    monkeypatch.setattr(shards, "is_current_public_polygon_schema", lambda *_args: True)
    monkeypatch.setattr(shards, "hash_file", lambda _path: "a" * 64)
    entry = {
        "filename": "a.osm.pbf",
        "public_row_count": 2,
        "public_shard_sha256": "a" * 64,
        "observation_row_count": 2,
        "observation_shard_sha256": "a" * 64,
        "rejection_count": 2,
        "rejection_shard_sha256": "a" * 64,
    }
    shards._verify_manifest_entry(tmp_path, entry, errors, checked, declared)
    assert errors == []
    assert declared == {"a"}
    assert checked == ["public:a", "comparison:a", "rejection:a"]

    errors.clear()
    monkeypatch.setattr(shards, "is_current_public_polygon_schema", lambda *_args: False)
    monkeypatch.setattr(shards, "schema_matches", lambda *_args: False)
    shards._verify_shard(tmp_path, "a", "a.osm.pbf", entry, contract, errors)
    assert errors == [f"exact schema mismatch in public shard {shard}"]

    errors.clear()
    shards._verify_row_count(1, "a.osm.pbf", contract, {"public_row_count": True}, errors)
    assert errors == ["invalid public_row_count for a.osm.pbf"]
    errors.clear()
    shards._verify_shard_hash(
        shard,
        "a.osm.pbf",
        contract,
        {"public_shard_sha256": "b" * 64},
        errors,
    )
    assert errors == [f"public shard hash mismatch for a.osm.pbf: {'a' * 64} != {'b' * 64}"]

    errors.clear()
    extra = tmp_path / contract.directory / "extra.parquet"
    extra.write_bytes(b"extra")
    shards._verify_extra_shards(tmp_path, contract, {"a"}, errors)
    assert errors == [f"extra undeclared public shard: {extra}"]


def test_shard_verification_reports_unreadable_and_invalid_hashes(
    tmp_path: Path, monkeypatch
) -> None:
    contract = shards.SHARD_CONTRACTS[0]
    shard = tmp_path / contract.directory / "a.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"bad")
    errors: list[str] = []
    monkeypatch.setattr(
        shards.pq,
        "ParquetFile",
        lambda _path: (_ for _ in ()).throw(RuntimeError("broken parquet")),
    )
    shards._verify_shard(
        tmp_path,
        "a",
        "a.osm.pbf",
        {"public_row_count": 0, "public_shard_sha256": "a" * 64},
        contract,
        errors,
    )
    assert errors == [f"unreadable public shard {shard}: broken parquet"]
    errors.clear()
    shards._verify_shard_hash(
        shard,
        "a.osm.pbf",
        contract,
        {"public_shard_sha256": "short"},
        errors,
    )
    assert errors == ["missing public shard hash for a.osm.pbf"]


def test_verification_entrypoints_and_nested_helpers_are_safe_on_incomplete_runs(
    tmp_path: Path,
) -> None:
    """Every release-gate boundary reports errors instead of raising."""
    errors: list[str] = []
    analysis.verify_analysis_and_card(tmp_path, errors)
    with suppress(FileNotFoundError, OSError):
        analysis._verify_analysis_arithmetic(tmp_path, errors)
    analysis._verify_card_files(tmp_path, errors)
    analysis._verify_analysis_inventory(tmp_path, errors)
    analysis._verify_analysis_readability(tmp_path, set(), errors)
    analysis._verify_card_statistics(tmp_path, errors)
    analysis._verify_map_artifact(tmp_path, errors)
    (tmp_path / "manifests").mkdir(exist_ok=True)
    (tmp_path / "manifests" / "sources.json").write_text(
        json.dumps([{"observation_row_count": 0}]), encoding="utf-8"
    )
    analysis._verify_observation_cells(tmp_path, [], set(), errors)
    analysis._verify_canonical_cells([], [], set(), errors)
    analysis._compare_card_file(tmp_path / "README.md", "expected", "README.md", errors)
    receipt.verify_receipt(tmp_path, errors)
    receipt._verify_card_contract(tmp_path, 1, errors)
    receipt._verify_receipt_artifacts(tmp_path, [], 1, errors)
    receipt._verify_receipt_entry(tmp_path, "not-a-dict", 1, set(), errors, [])
    receipt._verify_receipt_inventory(tmp_path, set(), errors)
    text._verify_text_shard(tmp_path / "missing.parquet", True, errors)
    text._verify_success_text("one two", 2, "website", errors)
    text._verify_empty_text("", 0, "website", errors)
    assert errors


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

    calls.clear()
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
    assert errors == [f"missing map artifact: {POLYGON_DENSITY_ASSET_REL_PATH}"]

    delegated: list[tuple[Path, object, list[str]]] = []
    monkeypatch.setattr(
        receipt,
        "_verify_card_contract",
        lambda root, version, received_errors: delegated.append((root, version, received_errors)),
    )
    errors.clear()
    receipt._verify_card_contract_before_card_refresh(tmp_path, 1, errors)
    assert delegated == [(tmp_path, 1, errors)]


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


def test_analysis_entrypoints_forward_the_selected_card_compatibility_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    errors: list[str] = []
    calls: list[tuple[str, object]] = []

    def expected_inventory(root: Path, received_errors: list[str]) -> None:
        calls.append(("expected", (root, received_errors)))

    def inventory(root: Path, received_errors: list[str]) -> tuple[set[str], set[str]]:
        calls.append(("inventory", (root, received_errors)))
        return {"a"}, {"a"}

    def card_files(root: Path, received_errors: list[str]) -> None:
        calls.append(("files", (root, received_errors)))

    def readability(root: Path, names: set[str], received_errors: list[str]) -> bool:
        calls.append(("readability", (root, names, received_errors)))
        return True

    monkeypatch.setattr(analysis, "_verify_expected_source_inventory", expected_inventory)
    monkeypatch.setattr(analysis, "_verify_analysis_inventory", inventory)
    monkeypatch.setattr(analysis, "_verify_card_files", card_files)
    monkeypatch.setattr(analysis, "_verify_analysis_readability", readability)
    monkeypatch.setattr(
        analysis,
        "_verify_analysis_arithmetic",
        lambda root, received_errors: calls.append(("arithmetic", (root, received_errors))),
    )
    monkeypatch.setattr(
        analysis,
        "_verify_card_statistics",
        lambda root, received_errors: calls.append(("card", (root, received_errors))),
    )
    monkeypatch.setattr(
        analysis,
        "_verify_release_card_statistics",
        lambda root, received_errors: calls.append(("release-card", (root, received_errors))),
    )
    monkeypatch.setattr(
        analysis,
        "_verify_map_artifact",
        lambda root, received_errors, *, require_readme_reference=True: calls.append(
            ("map", (root, received_errors, require_readme_reference))
        ),
    )

    analysis.verify_analysis_and_card(tmp_path, errors)
    assert calls == [
        ("expected", (tmp_path, errors)),
        ("inventory", (tmp_path, errors)),
        ("files", (tmp_path, errors)),
        ("readability", (tmp_path, {"a"}, errors)),
        ("arithmetic", (tmp_path, errors)),
        ("card", (tmp_path, errors)),
        ("map", (tmp_path, errors, True)),
    ]

    calls.clear()
    analysis.verify_release_analysis_and_card(tmp_path, errors)
    assert calls == [
        ("expected", (tmp_path, errors)),
        ("inventory", (tmp_path, errors)),
        ("files", (tmp_path, errors)),
        ("readability", (tmp_path, {"a"}, errors)),
        ("arithmetic", (tmp_path, errors)),
        ("release-card", (tmp_path, errors)),
        ("map", (tmp_path, errors, False)),
    ]


def test_analysis_skips_arithmetic_when_inventory_or_readability_is_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    errors: list[str] = []
    arithmetic_calls: list[object] = []
    monkeypatch.setattr(analysis, "_verify_expected_source_inventory", lambda *_args: None)
    monkeypatch.setattr(analysis, "_verify_card_files", lambda *_args: None)
    monkeypatch.setattr(analysis, "_verify_analysis_readability", lambda *_args: True)
    monkeypatch.setattr(
        analysis,
        "_verify_analysis_arithmetic",
        lambda *args: arithmetic_calls.append(args),
    )
    monkeypatch.setattr(
        analysis,
        "_verify_analysis_inventory",
        lambda *_args: ({"actual"}, {"expected"}),
    )
    monkeypatch.setattr(analysis, "_verify_card_statistics", lambda *_args: None)
    monkeypatch.setattr(analysis, "_verify_map_artifact", lambda *_args, **_kwargs: None)

    analysis.verify_analysis_and_card(tmp_path, errors)
    assert arithmetic_calls == []


def test_analysis_inventory_card_files_and_readability_report_exact_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    monkeypatch.setattr(analysis, "ANALYSIS_FILES", ("expected.parquet",))
    (analysis_dir / "expected.parquet").write_bytes(b"ok")
    (analysis_dir / "unexpected.parquet").write_bytes(b"extra")
    errors: list[str] = []
    assert analysis._verify_analysis_inventory(tmp_path, errors) == (
        {"expected.parquet", "unexpected.parquet"},
        {"expected.parquet"},
    )
    assert errors == ["unexpected analysis artifact: analysis/unexpected.parquet"]

    (analysis_dir / "expected.parquet").unlink()
    errors.clear()
    analysis._verify_analysis_inventory(tmp_path, errors)
    assert errors == [
        "missing analysis artifact: analysis/expected.parquet",
        "unexpected analysis artifact: analysis/unexpected.parquet",
    ]

    errors.clear()
    analysis._verify_card_files(tmp_path, errors)
    assert errors == [
        "missing card artifact: README.md",
        "missing card artifact: dataset.yaml",
        "missing card artifact: stats.json",
    ]
    for name in ("README.md", "dataset.yaml", "stats.json"):
        (tmp_path / name).write_text("ok", encoding="utf-8")
    errors.clear()
    analysis._verify_card_files(tmp_path, errors)
    assert errors == []

    expected_path = tmp_path / "manifests" / "expected_sources.json"
    expected_path.parent.mkdir()
    errors.clear()
    analysis._verify_expected_source_inventory(tmp_path, errors)
    assert errors == ["missing exact expected source inventory"]
    expected_path.write_text("[]", encoding="utf-8")
    errors.clear()
    analysis._verify_expected_source_inventory(tmp_path, errors)
    assert errors == []

    checked: list[Path] = []

    def parquet_file(path: Path) -> object:
        checked.append(path)
        if path.name == "broken.parquet":
            raise RuntimeError("broken")
        return object()

    monkeypatch.setattr(analysis.pq, "ParquetFile", parquet_file)
    (analysis_dir / "broken.parquet").write_bytes(b"broken")
    errors.clear()
    assert (
        analysis._verify_analysis_readability(tmp_path, {"broken.parquet", "ok.parquet"}, errors)
        is False
    )
    assert checked == [analysis_dir / "broken.parquet", analysis_dir / "ok.parquet"]
    assert errors == ["unreadable analysis artifact broken.parquet: broken"]


def test_card_file_and_map_verifiers_use_exact_paths_and_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "README.md"
    errors: list[str] = []
    analysis._compare_card_file(path, "expected", "README.md", errors)
    assert errors == []
    path.write_text("wrong", encoding="utf-8")
    analysis._compare_card_file(path, "expected", "README.md", errors)
    assert errors == ["README.md does not match artifact-derived statistics"]

    map_path = tmp_path / POLYGON_DENSITY_ASSET_REL_PATH
    errors.clear()
    analysis._verify_map_file(map_path, errors)
    assert errors == [f"missing map artifact: {POLYGON_DENSITY_ASSET_REL_PATH}"]
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"not-a-png")
    errors.clear()
    analysis._verify_map_file(map_path, errors)
    assert errors == ["map artifact is not a valid PNG"]
    map_path.write_bytes(b"\x89PNG\r\n\x1a\nextra")
    errors.clear()
    analysis._verify_map_file(map_path, errors)
    assert errors == []

    path.write_text("no map here", encoding="utf-8")
    errors.clear()
    analysis._verify_readme_map_reference(path, errors)
    assert errors == [f"README does not reference {POLYGON_DENSITY_ASSET_REL_PATH}"]
    path.write_text(POLYGON_DENSITY_ASSET_REL_PATH, encoding="utf-8")
    errors.clear()
    analysis._verify_readme_map_reference(path, errors)
    assert errors == []

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        analysis,
        "_verify_map_file",
        lambda received_path, received_errors: calls.append(
            ("file", (received_path, received_errors))
        ),
    )
    monkeypatch.setattr(
        analysis,
        "_verify_readme_map_reference",
        lambda received_path, received_errors: calls.append(
            ("readme", (received_path, received_errors))
        ),
    )
    analysis._verify_map_artifact(tmp_path, errors)
    assert calls == [
        ("file", (map_path, errors)),
        ("readme", (tmp_path / "README.md", errors)),
    ]
    calls.clear()
    analysis._verify_map_artifact(tmp_path, errors, require_readme_reference=False)
    assert calls == [("file", (map_path, errors))]


def test_analysis_arithmetic_forwards_exact_eight_cell_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells = [
        {"cell": cell, "level": level, "row_count": 1}
        for level in ("observation", "canonical")
        for cell in (
            "cell_000_w0_c0_d0",
            "cell_001_w0_c0_d1",
            "cell_010_w0_c1_d0",
            "cell_011_w0_c1_d1",
            "cell_100_w1_c0_d0",
            "cell_101_w1_c0_d1",
            "cell_110_w1_c1_d0",
            "cell_111_w1_c1_d1",
        )
    ]
    captured: list[Path] = []
    monkeypatch.setattr(
        analysis.pq,
        "read_table",
        lambda path: captured.append(path) or SimpleNamespace(to_pylist=lambda: cells),
    )
    observations: list[object] = []
    canonicals: list[object] = []
    monkeypatch.setattr(
        analysis,
        "_verify_observation_cells",
        lambda root, rows, expected, errors: observations.append((root, rows, expected, errors)),
    )
    monkeypatch.setattr(
        analysis,
        "_verify_canonical_cells",
        lambda rows, observation_rows, expected, errors: canonicals.append(
            (rows, observation_rows, expected, errors)
        ),
    )
    errors: list[str] = []
    analysis._verify_analysis_arithmetic(tmp_path, errors)
    expected = {
        "cell_000_w0_c0_d0",
        "cell_001_w0_c0_d1",
        "cell_010_w0_c1_d0",
        "cell_011_w0_c1_d1",
        "cell_100_w1_c0_d0",
        "cell_101_w1_c0_d1",
        "cell_110_w1_c1_d0",
        "cell_111_w1_c1_d1",
    }
    assert captured == [tmp_path / "analysis" / "cells_global.parquet"]
    assert observations == [(tmp_path, cells[:8], expected, errors)]
    assert canonicals == [(cells[8:], cells[:8], expected, errors)]


def test_analysis_cell_and_total_helpers_distinguish_exact_and_boundary_values(
    tmp_path: Path,
) -> None:
    expected = {"a", "b"}
    errors: list[str] = []
    assert analysis._verify_cell_set(
        [{"cell": "a"}, {"cell": "b"}], "observation", expected, errors
    )
    errors.clear()
    assert not analysis._verify_cell_set([{"cell": "a"}], "canonical", expected, errors)
    assert errors == ["canonical analysis does not contain exactly eight cells"]

    manifest = tmp_path / "manifests" / "sources.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps([{"observation_row_count": 2}]), encoding="utf-8")
    errors.clear()
    analysis._verify_observation_total(tmp_path, [{"row_count": 1}, {"row_count": 1}], errors)
    assert errors == []
    analysis._verify_observation_total(tmp_path, [{"row_count": 1}], errors)
    assert errors == ["observation cell total mismatch: 1 != 2"]

    errors.clear()
    analysis._verify_canonical_total([{"row_count": 2}], [{"row_count": 2}], errors)
    assert errors == []
    analysis._verify_canonical_total([{"row_count": 3}], [{"row_count": 2}], errors)
    assert errors == ["canonical cell total exceeds observation total"]


def test_card_statistics_verifiers_forward_all_artifact_renderers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats = object()
    geometry = GeometryStats(row_count=2)
    calls: list[tuple[str, object]] = []
    errors: list[str] = []
    monkeypatch.setattr(
        analysis,
        "compute_card_stats",
        lambda root: calls.append(("stats", root)) or stats,
    )
    monkeypatch.setattr(
        analysis,
        "compute_geometry_stats",
        lambda root: calls.append(("geometry", root)) or geometry,
    )
    monkeypatch.setattr(
        analysis,
        "_render_yaml_front_matter",
        lambda received: calls.append(("yaml", received)) or "yaml",
    )
    monkeypatch.setattr(
        analysis,
        "_public_schema_for_card",
        lambda root: calls.append(("schema", root)) or "schema",
    )
    monkeypatch.setattr(
        analysis,
        "_render_markdown",
        lambda received, *, geometry, schema: (
            calls.append(("markdown", (received, geometry, schema))) or "readme"
        ),
    )
    monkeypatch.setattr(
        analysis,
        "render_geometry_stats",
        lambda received: calls.append(("geometry-render", received)) or "stats-json",
    )
    compared: list[tuple[Path, str, str, list[str]]] = []
    monkeypatch.setattr(
        analysis,
        "_compare_card_file",
        lambda path, expected, label, received_errors: compared.append(
            (path, expected, label, received_errors)
        ),
    )
    analysis._verify_card_statistics(tmp_path, errors)
    assert errors == []
    assert calls == [
        ("stats", tmp_path),
        ("geometry", tmp_path),
        ("yaml", stats),
        ("schema", tmp_path),
        ("markdown", (stats, geometry, "schema")),
        ("geometry-render", geometry),
    ]
    assert compared == [
        (tmp_path / "dataset.yaml", "yaml", "dataset.yaml", errors),
        (tmp_path / "README.md", "yaml\nreadme", "README.md", errors),
        (tmp_path / "stats.json", "stats-json", "stats.json", errors),
    ]


def test_release_card_statistics_and_geometry_section_are_exact_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = GeometryStats(row_count=2)
    calls: list[tuple[str, object]] = []
    compared: list[tuple[Path, str, str, list[str]]] = []
    errors: list[str] = []
    monkeypatch.setattr(
        analysis,
        "compute_geometry_stats",
        lambda root: calls.append(("geometry", root)) or geometry,
    )
    monkeypatch.setattr(
        analysis,
        "render_geometry_stats",
        lambda received: calls.append(("render", received)) or "stats",
    )
    monkeypatch.setattr(
        analysis,
        "_verify_release_geometry_section",
        lambda root, received, received_errors: calls.append(
            ("section", (root, received, received_errors))
        ),
    )
    monkeypatch.setattr(
        analysis,
        "_compare_card_file",
        lambda path, expected, label, received_errors: compared.append(
            (path, expected, label, received_errors)
        ),
    )
    analysis._verify_release_card_statistics(tmp_path, errors)
    assert errors == []
    assert calls == [
        ("geometry", tmp_path),
        ("render", geometry),
        ("section", (tmp_path, geometry, errors)),
    ]
    assert compared == [(tmp_path / "stats.json", "stats", "stats.json", errors)]

    monkeypatch.setattr(
        analysis, "compute_geometry_stats", lambda _root: (_ for _ in ()).throw(RuntimeError("bad"))
    )
    errors.clear()
    analysis._verify_release_card_statistics(tmp_path, errors)
    assert errors == ["release card statistic verification failed: bad"]


def test_release_geometry_section_normalizes_newlines_and_requires_exact_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = object()
    lines = ["## Polygon geometry", "", "derived", ""]
    monkeypatch.setattr(analysis, "_render_polygon_geometry_section", lambda _geometry: lines)
    readme = tmp_path / "README.md"
    readme.write_bytes(b"prefix\r\n## Polygon geometry\r\n\r\nderived\r\n\r\n## Next\r\n")
    errors: list[str] = []
    analysis._verify_release_geometry_section(tmp_path, geometry, errors)
    assert errors == []

    readme.write_bytes(b"prefix\n## Polygon geometry\nwrong\n## Next\n")
    analysis._verify_release_geometry_section(tmp_path, geometry, errors)
    assert errors == ["README Polygon geometry section does not match artifact-derived statistics"]

    errors.clear()
    readme.write_bytes(b"prefix\n## Other\nvalue\n")
    analysis._verify_release_geometry_section(tmp_path, geometry, errors)
    assert errors == ["README Polygon geometry section does not match artifact-derived statistics"]

    errors.clear()
    readme.write_bytes(b"\xff")
    analysis._verify_release_geometry_section(tmp_path, geometry, errors)
    assert errors and errors[0].startswith("README geometry section is unreadable: ")
