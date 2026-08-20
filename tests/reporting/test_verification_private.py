"""Direct contracts for verification helpers used by the release gate."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path

import duckdb

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
    receipt._verify_receipt_digest(
        {
            "manifest_digest": hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        },
        canonical,
        errors,
    )
    assert receipt._read_receipt(tmp_path / "missing.json", errors) == {}
    receipt._verify_current_card_contract(tmp_path / "map.png", errors)
    receipt._verify_legacy_card_contract(tmp_path / "map.png", errors)
    assert any("missing map artifact" in error for error in errors)


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
