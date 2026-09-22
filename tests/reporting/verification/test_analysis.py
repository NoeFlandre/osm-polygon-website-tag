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
    monkeypatch.setattr(
        analysis,
        "compute_geometry_stats",
        lambda root, **kwargs: calls.append(("geometry", (root, sorted(kwargs)))) or geometry,
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
    monkeypatch.setattr(analysis, "_verify_text_population_agreement", lambda *args: None)
    analysis._verify_card_statistics(tmp_path, errors)
    assert errors == []
    assert calls == [
        ("stats", (tmp_path, ["summary", "text_population"])),
        ("geometry", (tmp_path, ["text_population"])),
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
        lambda root, **kwargs: calls.append(("geometry", (root, sorted(kwargs)))) or geometry,
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
    monkeypatch.setattr(
        analysis, "compute_polygon_density_summary", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(analysis, "compute_card_stats", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(analysis, "_verify_release_geographic_section", lambda *_args: None)
    monkeypatch.setattr(analysis, "_verify_release_density_yaml", lambda *_args: None)
    monkeypatch.setattr(analysis, "_verify_text_population_agreement", lambda *args: None)
    analysis._verify_release_card_statistics(tmp_path, errors)
    assert errors == []
    assert calls == [
        ("geometry", (tmp_path, ["text_population"])),
        ("render", geometry),
        ("section", (tmp_path, geometry, errors)),
    ]
    assert compared == [(tmp_path / "stats.json", "stats", "stats.json", errors)]

    monkeypatch.setattr(
        analysis,
        "compute_geometry_stats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad")),
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


def test_map_verifier_rejects_bytes_from_a_different_global_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    map_path = tmp_path / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir()
    map_path.write_bytes(b"actual")

    def fake_render(_summary: object, output_path: Path) -> str:
        output_path.write_bytes(b"expected")
        return "caption"

    monkeypatch.setattr(analysis, "render_polygon_density", fake_render)
    errors: list[str] = []

    analysis._verify_map_matches_summary(tmp_path, object(), errors)

    assert errors == ["map artifact does not match the canonical global summary"]


def test_observation_cells_are_named_observation_in_their_error(tmp_path: Path) -> None:
    """The level label is what tells the two cell errors apart."""
    errors: list[str] = []

    analysis._verify_observation_cells(tmp_path, _cells({"a": 1}), set(_EIGHT), errors)

    assert errors == ["observation analysis does not contain exactly eight cells"]


def test_canonical_cells_are_named_canonical_in_their_error() -> None:
    errors: list[str] = []

    analysis._verify_canonical_cells(_cells({"a": 1}), _cells({"a": 1}), set(_EIGHT), errors)

    assert errors == ["canonical analysis does not contain exactly eight cells"]


def test_observation_cells_check_their_total_once_the_set_matches(tmp_path: Path) -> None:
    """A wrong total must surface, which only happens if the guard passed."""
    _sources_manifest(tmp_path, observation_total=99)
    errors: list[str] = []

    analysis._verify_observation_cells(tmp_path, _cells(_EIGHT), set(_EIGHT), errors)

    assert errors == ["observation cell total mismatch: 8 != 99"]


def test_observation_cells_accept_a_matching_total(tmp_path: Path) -> None:
    _sources_manifest(tmp_path, observation_total=8)
    errors: list[str] = []

    analysis._verify_observation_cells(tmp_path, _cells(_EIGHT), set(_EIGHT), errors)

    assert errors == []


def test_a_wrong_cell_set_skips_the_observation_total(tmp_path: Path) -> None:
    """No manifest is written, so reading one would raise rather than report."""
    errors: list[str] = []

    analysis._verify_observation_cells(tmp_path, _cells({"a": 1}), set(_EIGHT), errors)

    assert errors == ["observation analysis does not contain exactly eight cells"]


def test_canonical_cells_check_their_total_once_the_set_matches() -> None:
    errors: list[str] = []

    analysis._verify_canonical_cells(
        _cells(dict.fromkeys(_EIGHT, 5)), _cells(_EIGHT), set(_EIGHT), errors
    )

    assert errors == ["canonical cell total exceeds observation total"]


def test_canonical_cells_accept_a_total_within_the_observations() -> None:
    errors: list[str] = []

    analysis._verify_canonical_cells(
        _cells(_EIGHT), _cells(dict.fromkeys(_EIGHT, 5)), set(_EIGHT), errors
    )

    assert errors == []


def test_yaml_fields_report_a_missing_key_when_it_is_required() -> None:
    errors: list[str] = []

    analysis._verify_release_yaml_fields("other: 1\n", "dataset.yaml", {"count": 3}, errors)

    assert errors == ["dataset.yaml count does not match canonical text statistics"]


def test_yaml_fields_ignore_a_missing_key_when_it_is_optional() -> None:
    """A preserved card may predate a field; only a present one must agree."""
    errors: list[str] = []

    analysis._verify_release_yaml_fields(
        "other: 1\n", "dataset.yaml", {"count": 3}, errors, require_present=False
    )

    assert errors == []


def test_an_optional_yaml_field_still_has_to_agree_when_present() -> None:
    errors: list[str] = []

    analysis._verify_release_yaml_fields(
        "count: 9\n", "dataset.yaml", {"count": 3}, errors, require_present=False
    )

    assert errors == ["dataset.yaml count does not match canonical text statistics"]


def _recording_release_card_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, tuple[object, ...]]], dict[str, object]]:
    """Stub every collaborator of both card-statistic gates and record exact arguments."""
    calls: list[tuple[str, tuple[object, ...]]] = []
    values = {name: object() for name in ("population", "summary", "stats", "geometry")}

    def record(name: str, result: object = None):
        def stub(*args: object, **kwargs: object) -> object:
            calls.append((name, (*args, *sorted(kwargs.items()))))
            return result

        return stub

    monkeypatch.setattr(
        analysis, "compute_text_population_summary", record("population", values["population"])
    )
    monkeypatch.setattr(
        analysis, "compute_polygon_density_summary", record("summary", values["summary"])
    )
    monkeypatch.setattr(analysis, "compute_card_stats", record("stats", values["stats"]))
    monkeypatch.setattr(analysis, "compute_geometry_stats", record("geometry", values["geometry"]))
    monkeypatch.setattr(analysis, "render_geometry_stats", record("render", "stats-text"))
    monkeypatch.setattr(analysis, "_render_yaml_front_matter", record("yaml", "yaml-text"))
    monkeypatch.setattr(analysis, "_render_markdown", record("markdown", "markdown-text"))
    monkeypatch.setattr(analysis, "_public_schema_for_card", record("schema", "schema"))
    for name in (
        "_compare_card_file",
        "_verify_release_geometry_section",
        "_verify_text_population_agreement",
        "_verify_map_matches_summary",
        "_verify_release_website_text_section",
        "_verify_release_text_yaml",
        "_verify_release_geographic_section",
        "_verify_release_density_yaml",
    ):
        monkeypatch.setattr(analysis, name, record(name))
    return calls, values


def test_release_card_statistics_hands_each_gate_the_shared_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, values = _recording_release_card_pipeline(monkeypatch)
    population, summary = values["population"], values["summary"]
    stats, geometry = values["stats"], values["geometry"]
    errors: list[str] = []

    analysis._verify_release_card_statistics(tmp_path, errors)

    assert errors == []
    assert calls == [
        ("population", (tmp_path,)),
        ("summary", (tmp_path, ("aggregation_mode", "global_unique_text"))),
        ("geometry", (tmp_path, ("text_population", population))),
        ("render", (geometry,)),
        ("_compare_card_file", (tmp_path / "stats.json", "stats-text", "stats.json", errors)),
        ("_verify_release_geometry_section", (tmp_path, geometry, errors)),
        ("stats", (tmp_path, ("summary", summary), ("text_population", population))),
        (
            "_verify_text_population_agreement",
            (population, summary, stats, geometry, errors),
        ),
        ("_verify_map_matches_summary", (tmp_path, summary, errors)),
        ("_verify_release_website_text_section", (tmp_path, stats, errors)),
        ("_verify_release_text_yaml", (tmp_path, stats, errors)),
        ("_verify_release_geographic_section", (tmp_path, stats, errors)),
        ("_verify_release_density_yaml", (tmp_path, stats, errors)),
    ]
    for _name, args in calls[4:]:
        if errors in args:
            assert args[-1] is errors


def test_card_statistics_hands_each_gate_the_shared_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, values = _recording_release_card_pipeline(monkeypatch)
    population, summary = values["population"], values["summary"]
    stats, geometry = values["stats"], values["geometry"]
    errors: list[str] = []

    analysis._verify_card_statistics(tmp_path, errors)

    assert errors == []
    assert calls == [
        ("population", (tmp_path,)),
        ("summary", (tmp_path, ("aggregation_mode", "global_unique_text"))),
        ("stats", (tmp_path, ("summary", summary), ("text_population", population))),
        ("geometry", (tmp_path, ("text_population", population))),
        (
            "_verify_text_population_agreement",
            (population, summary, stats, geometry, errors),
        ),
        ("_verify_map_matches_summary", (tmp_path, summary, errors)),
        ("yaml", (stats,)),
        ("schema", (tmp_path,)),
        ("markdown", (stats, ("geometry", geometry), ("schema", "schema"))),
        ("_compare_card_file", (tmp_path / "dataset.yaml", "yaml-text", "dataset.yaml", errors)),
        (
            "_compare_card_file",
            (tmp_path / "README.md", "yaml-text\nmarkdown-text", "README.md", errors),
        ),
        ("render", (geometry,)),
        ("_compare_card_file", (tmp_path / "stats.json", "stats-text", "stats.json", errors)),
    ]
    assert calls[4][1][-1] is errors
    assert calls[5][1][-1] is errors


def test_card_statistics_reports_a_failed_computation_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_root: Path) -> None:
        raise RuntimeError("unreadable shard")

    monkeypatch.setattr(analysis, "compute_text_population_summary", fail)
    errors: list[str] = []

    analysis._verify_card_statistics(tmp_path, errors)

    assert errors == ["card statistic verification failed: unreadable shard"]


def _text_stats() -> SimpleNamespace:
    return SimpleNamespace(
        website_text_success_count=11,
        website_total_words=22,
        contact_website_text_success_count=33,
        contact_website_total_words=44,
        polygons_with_any_text=55,
    )


_TEXT_EXPECTED = {
    "website_text_success_count": 11,
    "website_total_words": 22,
    "contact_website_text_success_count": 33,
    "contact_website_total_words": 44,
    "unique_text_identity_count": 55,
}


@pytest.mark.parametrize(
    "present", [("dataset.yaml",), ("README.md",), ("dataset.yaml", "README.md")]
)
def test_release_text_yaml_checks_both_files_when_either_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    present: tuple[str, ...],
) -> None:
    for name in present:
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        analysis, "_verify_release_yaml_path", lambda *args: calls.append(("yaml", args))
    )
    monkeypatch.setattr(
        analysis, "_verify_release_readme", lambda *args: calls.append(("readme", args))
    )
    stats = _text_stats()
    errors: list[str] = []

    analysis._verify_release_text_yaml(tmp_path, stats, errors)

    assert calls == [
        ("yaml", (tmp_path / "dataset.yaml", "dataset.yaml", _TEXT_EXPECTED, errors)),
        ("readme", (tmp_path / "README.md", stats, _TEXT_EXPECTED, errors)),
    ]
    assert calls[0][1][3] is errors
    assert calls[1][1][3] is errors
    assert str(calls[0][1][0]).endswith("/dataset.yaml")
    assert str(calls[1][1][0]).endswith("/README.md")


def test_release_text_yaml_skips_a_run_without_either_card_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(analysis, "_verify_release_yaml_path", lambda *args: calls.append(args))
    monkeypatch.setattr(analysis, "_verify_release_readme", lambda *args: calls.append(args))

    analysis._verify_release_text_yaml(tmp_path, _text_stats(), [])

    assert calls == []


def test_release_yaml_path_ignores_a_missing_file(tmp_path: Path) -> None:
    errors: list[str] = []

    analysis._verify_release_yaml_path(tmp_path / "dataset.yaml", "dataset.yaml", {"a": 1}, errors)

    assert errors == []


def test_release_yaml_path_compares_the_file_contents(tmp_path: Path) -> None:
    path = tmp_path / "dataset.yaml"
    path.write_text("a: 1\nb: 9\n", encoding="utf-8")
    errors: list[str] = []

    analysis._verify_release_yaml_path(path, "label.yaml", {"a": 1, "b": 2}, errors)

    assert errors == ["label.yaml b does not match canonical text statistics"]


def test_release_yaml_path_reports_undecodable_bytes(tmp_path: Path) -> None:
    path = tmp_path / "dataset.yaml"
    path.write_bytes(b"\xff")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        expected = f"label.yaml text fields are unreadable: {exc}"
    errors: list[str] = []

    analysis._verify_release_yaml_path(path, "label.yaml", {"a": 1}, errors)

    assert errors == [expected]


def _no_language_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analysis, "_render_language_section", lambda _stats: ["## Languages", ""])


def test_release_readme_ignores_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_language_section(monkeypatch)
    errors: list[str] = []

    analysis._verify_release_readme(tmp_path / "README.md", object(), {"a": 1}, errors)

    assert errors == []


def test_release_readme_reports_undecodable_bytes(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_bytes(b"\xff")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        expected = f"README front matter text fields are unreadable: {exc}"
    errors: list[str] = []

    analysis._verify_release_readme(path, object(), {"a": 1}, errors)

    assert errors == [expected]


def test_release_readme_checks_only_multiline_front_matter_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_language_section(monkeypatch)
    path = tmp_path / "README.md"
    path.write_text(
        "---\nname: x\nwebsite_total_words: 9\n---\n\nwebsite_total_words: 3\nb: 7\n",
        encoding="utf-8",
    )
    errors: list[str] = []

    analysis._verify_release_readme(path, object(), {"website_total_words": 3, "b": 8}, errors)

    assert errors == [
        "README front matter website_total_words does not match canonical text statistics"
    ]


def test_release_readme_without_front_matter_reports_no_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_language_section(monkeypatch)
    path = tmp_path / "README.md"
    path.write_text("website_total_words: 9\n", encoding="utf-8")
    errors: list[str] = []

    analysis._verify_release_readme(path, object(), {"website_total_words": 3}, errors)

    assert errors == []


def test_release_readme_hands_the_whole_card_to_the_language_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        analysis, "_verify_release_language_section", lambda *args: calls.append(args)
    )
    path = tmp_path / "README.md"
    path.write_text("---\na: 1\n---\nbody\n", encoding="utf-8")
    stats = object()
    errors: list[str] = []

    analysis._verify_release_readme(path, stats, {"a": 1}, errors)

    assert calls == [("---\na: 1\n---\nbody\n", stats, errors)]
    assert calls[0][1] is stats
    assert calls[0][2] is errors


_LANGUAGE_LINES = ["## Languages", "", "- en: 3", ""]
_LANGUAGE_ERROR = "README Languages section does not match canonical text statistics"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("intro\n## Languages\n\n- en: 3\n\n## Next\nx\n", []),
        ("intro\r\n## Languages\r\n\r\n- en: 3\r\n\r\n## Next\r\nx\r\n", []),
        ("intro\n## Languages\n\n- en: 4\n\n## Next\nx\n", [_LANGUAGE_ERROR]),
        ("intro\r\n## Languages\r\n\r\n- en: 4\r\n", [_LANGUAGE_ERROR]),
        ("intro\n## Other\n\n- en: 4\n", []),
    ],
)
def test_release_language_section_compares_an_existing_section(
    monkeypatch: pytest.MonkeyPatch, content: str, expected: list[str]
) -> None:
    stats = object()
    seen: list[object] = []
    monkeypatch.setattr(
        analysis,
        "_render_language_section",
        lambda received: seen.append(received) or _LANGUAGE_LINES,
    )
    errors: list[str] = []

    analysis._verify_release_language_section(content, stats, errors)

    assert errors == expected
    assert all(received is stats for received in seen)


_WEBSITE_LINES = ["## Website text", "", "- words: 3", ""]
_WEBSITE_ERROR = "README Website text section does not match canonical text statistics"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"intro\n## Website text\n\n- words: 3\n\n## Next\n", []),
        (b"intro\r\n## Website text\r\n\r\n- words: 3\r\n\r\n## Next\r\n", []),
        (b"intro\n## Website text\n\n- words: 4\n\n## Next\n", [_WEBSITE_ERROR]),
        (b"intro\r\n## Website text\r\n\r\n- words: 4\r\n", [_WEBSITE_ERROR]),
        (b"intro\n## Website text", [_WEBSITE_ERROR]),
        (b"intro\n## Other\n", []),
    ],
)
def test_release_website_text_section_compares_an_existing_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes, expected: list[str]
) -> None:
    monkeypatch.setattr(analysis, "_render_website_text_section", lambda _stats: _WEBSITE_LINES)
    (tmp_path / "README.md").write_bytes(content)
    errors: list[str] = []

    analysis._verify_release_website_text_section(tmp_path, object(), errors)

    assert errors == expected


def test_release_website_text_section_reads_the_card_by_its_exact_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        opened.append(path.name)
        return real_read_bytes(path)

    monkeypatch.setattr(analysis, "_render_website_text_section", lambda _stats: _WEBSITE_LINES)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    (tmp_path / "README.md").write_bytes(b"## Website text\n\n- words: 4\n")
    errors: list[str] = []

    analysis._verify_release_website_text_section(tmp_path, object(), errors)

    assert opened == ["README.md"]
    assert errors == [_WEBSITE_ERROR]


_GEOGRAPHIC_LINES = ["## Geographic distribution", "", "- cells: 3", ""]
_GEOGRAPHIC_ERROR = "README Geographic distribution section does not match the unique-text summary"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"intro\n## Geographic distribution\n\n- cells: 3\n\n## Next\n", []),
        (b"intro\r\n## Geographic distribution\r\n\r\n- cells: 3\r\n\r\n## Next\r\n", []),
        (b"intro\n## Geographic distribution\n\n- cells: 4\n", [_GEOGRAPHIC_ERROR]),
        (b"intro\n## GEOGRAPHIC DISTRIBUTION\n\n- cells: 3\n", [_GEOGRAPHIC_ERROR]),
    ],
)
def test_release_geographic_section_compares_the_exact_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes, expected: list[str]
) -> None:
    monkeypatch.setattr(analysis, "_render_geographic_section", lambda _stats: _GEOGRAPHIC_LINES)
    (tmp_path / "README.md").write_bytes(content)
    errors: list[str] = []

    analysis._verify_release_geographic_section(tmp_path, object(), errors)

    assert errors == expected


def test_readability_of_no_artifacts_is_true(tmp_path: Path) -> None:
    assert analysis._verify_analysis_readability(tmp_path, set(), []) is True


def test_analysis_reads_only_artifacts_both_present_and_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[set[str]] = []
    monkeypatch.setattr(analysis, "_verify_expected_source_inventory", lambda *_args: None)
    monkeypatch.setattr(
        analysis, "_verify_analysis_inventory", lambda *_args: ({"a", "b"}, {"b", "c"})
    )
    monkeypatch.setattr(analysis, "_verify_card_files", lambda *_args: None)
    monkeypatch.setattr(
        analysis,
        "_verify_analysis_readability",
        lambda _root, names, _errors: seen.append(names) or True,
    )
    monkeypatch.setattr(analysis, "_verify_card_statistics", lambda *_args: None)
    monkeypatch.setattr(analysis, "_verify_map_artifact", lambda *_args, **_kwargs: None)

    analysis.verify_analysis_and_card(tmp_path, [])

    assert seen == [{"b"}]
