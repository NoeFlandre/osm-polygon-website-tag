"""Focused contracts for private analysis verification helpers."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geometry_stats import GeometryStats
from osm_polygon_website_tag.reporting.verification import analysis, receipt, rows, text


def _cells(counts: dict[str, int]) -> list[dict[str, object]]:
    return [{"cell": cell, "row_count": count} for cell, count in counts.items()]


_EIGHT = {f"c{index}": 1 for index in range(8)}


def _sources_manifest(root: Path, observation_total: int) -> None:
    (root / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "manifests" / "sources.json").write_text(
        json.dumps([{"observation_row_count": observation_total}]), encoding="utf-8"
    )


def test_analysis_and_row_verification_helpers_are_deterministic(tmp_path: Path) -> None:
    errors: list[str] = []
    expected = {"a", "b"}
    assert analysis._verify_cell_set(
        [{"cell": "a"}, {"cell": "b"}], "observation", expected, errors
    )
    assert not analysis._verify_cell_set([{"cell": "a"}], "canonical", expected, errors)
    assert errors == ["canonical analysis does not contain exactly eight cells"]
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
    for message in (
        "missing exact expected source inventory",
        "missing card artifact: README.md",
        "completion receipt has no artifact list",
        "receipt has stale card_contract_version: 1",
        "invalid completion receipt artifact entry",
        "completion receipt artifact inventory mismatch",
    ):
        assert message in errors
    assert errors[-1].startswith("text invariant verification failed for missing.parquet: ")


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
        lambda root, **kwargs: calls.append(("stats", (root, sorted(kwargs)))) or stats,
    )
    monkeypat