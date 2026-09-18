"""Validation of derived analysis, card, and map artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pyarrow.parquet as pq

from osm_polygon_website_tag.pipeline.analyze import ANALYSIS_FILES
from osm_polygon_website_tag.reporting.card import (
    _public_schema_for_card,
    _render_geographic_section,
    _render_language_section,
    _render_markdown,
    _render_polygon_geometry_section,
    _render_website_text_section,
    _render_yaml_front_matter,
)
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
from osm_polygon_website_tag.reporting.geographic.aggregation import (
    compute_polygon_density_summary,
)
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geographic.rendering import render_polygon_density
from osm_polygon_website_tag.reporting.geometry_stats import (
    GEOMETRY_STATS_FILENAME,
    compute_geometry_stats,
    render_geometry_stats,
)
from osm_polygon_website_tag.reporting.text_population import (
    TextPopulationSummary,
    compute_text_population_summary,
)


def verify_analysis_and_card(root: Path, errors: list[str]) -> None:
    """Verify derived analysis files and deterministic card/map output."""
    _verify_analysis_and_card(root, errors, preserve_card_sections=False)


def verify_release_analysis_and_card(root: Path, errors: list[str]) -> None:
    """Verify release-derived metadata while preserving legacy card sections."""
    _verify_analysis_and_card(root, errors, preserve_card_sections=True)


def _verify_analysis_and_card(
    root: Path,
    errors: list[str],
    *,
    preserve_card_sections: bool,
) -> None:
    """Run shared analysis checks with the selected card compatibility contract."""
    _verify_expected_source_inventory(root, errors)
    actual, expected = _verify_analysis_inventory(root, errors)
    _verify_card_files(root, errors)
    readable = _verify_analysis_readability(root, actual & expected, errors)
    if actual == expected and readable:
        try:
            _verify_analysis_arithmetic(root, errors)
        except Exception as exc:
            errors.append(f"analysis arithmetic verification failed: {exc}")
    if preserve_card_sections:
        _verify_release_card_statistics(root, errors)
        _verify_map_artifact(root, errors, require_readme_reference=False)
    else:
        _verify_card_statistics(root, errors)
        _verify_map_artifact(root, errors)


def _verify_expected_source_inventory(root: Path, errors: list[str]) -> None:
    if not (root / "manifests" / "expected_sources.json").is_file():
        errors.append("missing exact expected source inventory")


def _verify_analysis_inventory(
    root: Path,
    errors: list[str],
) -> tuple[set[str], set[str]]:
    actual = {path.name for path in (root / "analysis").glob("*.parquet")}
    expected = set(ANALYSIS_FILES)
    for name in sorted(expected - actual):
        errors.append(f"missing analysis artifact: analysis/{name}")
    for name in sorted(actual - expected):
        errors.append(f"unexpected analysis artifact: analysis/{name}")
    return actual, expected


def _verify_card_files(root: Path, errors: list[str]) -> None:
    for name in ("README.md", "dataset.yaml", GEOMETRY_STATS_FILENAME):
        if not (root / name).is_file():
            errors.append(f"missing card artifact: {name}")


def _verify_analysis_readability(
    root: Path,
    names: set[str],
    errors: list[str],
) -> bool:
    readable = True
    for name in sorted(names):
        try:
            pq.ParquetFile(root / "analysis" / name)
        except Exception as exc:
            readable = False
            errors.append(f"unreadable analysis artifact {name}: {exc}")
    return readable


def _verify_card_statistics(root: Path, errors: list[str]) -> None:
    try:
        text_population = compute_text_population_summary(root)
        summary = compute_polygon_density_summary(root, aggregation_mode="global_unique_text")
        stats = compute_card_stats(root, summary=summary, text_population=text_population)
        geometry = compute_geometry_stats(root, text_population=text_population)
        _verify_text_population_agreement(text_population, summary, stats, geometry, errors)
        _verify_map_matches_summary(root, summary, errors)
        expected_yaml = _render_yaml_front_matter(stats)
        expected_readme = (
            expected_yaml
            + "\n"
            + _render_markdown(stats, geometry=geometry, schema=_public_schema_for_card(root))
        )
        _compare_card_file(root / "dataset.yaml", expected_yaml, "dataset.yaml", errors)
        _compare_card_file(root / "README.md", expected_readme, "README.md", errors)
        _compare_card_file(
            root / GEOMETRY_STATS_FILENAME,
            render_geometry_stats(geometry),
            GEOMETRY_STATS_FILENAME,
            errors,
        )
    except Exception as exc:
        errors.append(f"card statistic verification failed: {exc}")


def _verify_release_card_statistics(root: Path, errors: list[str]) -> None:
    """Verify release-derived card values without rewriting legacy sections."""
    try:
        text_population = compute_text_population_summary(root)
        summary = compute_polygon_density_summary(root, aggregation_mode="global_unique_text")
        geometry = compute_geometry_stats(root, text_population=text_population)
        _compare_card_file(
            root / GEOMETRY_STATS_FILENAME,
            render_geometry_stats(geometry),
            GEOMETRY_STATS_FILENAME,
            errors,
        )
        _verify_release_geometry_section(root, geometry, errors)
        stats = compute_card_stats(root, summary=summary, text_population=text_population)
        _verify_text_population_agreement(text_population, summary, stats, geometry, errors)
        _verify_map_matches_summary(root, summary, errors)
        _verify_release_website_text_section(root, stats, errors)
        _verify_release_text_yaml(root, stats, errors)
        _verify_release_geographic_section(root, stats, errors)
        _verify_release_density_yaml(root, stats, errors)
    except Exception as exc:
        errors.append(f"release card statistic verification failed: {exc}")


def _verify_text_population_agreement(
    text_population: TextPopulationSummary,
    summary: Any,
    stats: Any,
    geometry: Any,
    errors: list[str],
) -> None:
    """Require card, map, and machine-readable text populations to agree."""
    expected = text_population.unique_identity_count
    if summary.polygon_row_count != expected:
        errors.append(
            "global map population does not match text report: "
            f"{summary.polygon_row_count} != {expected}"
        )
    if stats.polygons_with_any_text != expected:
        errors.append(
            "card unique-text population does not match text report: "
            f"{stats.polygons_with_any_text} != {expected}"
        )
    if geometry.text_population != text_population:
        errors.append("stats.json text population does not match the canonical text report")


def _verify_map_matches_summary(root: Path, summary: Any, errors: list[str]) -> None:
    """Re-render an existing map and reject bytes from another population."""
    map_path = root / POLYGON_DENSITY_ASSET_REL_PATH
    if not map_path.is_file():
        return
    try:
        with TemporaryDirectory(dir=map_path.parent) as temporary:
            expected_path = Path(temporary) / "expected.png"
            render_polygon_density(summary, expected_path)
            if map_path.read_bytes() != expected_path.read_bytes():
                errors.append("map artifact does not match the canonical global summary")
    except Exception as exc:
        errors.append(f"map artifact verification failed: {exc}")


def _verify_release_geometry_section(
    root: Path,
    geometry: Any,
    errors: list[str],
) -> None:
    """Require the additive geometry section to match artifact-derived values."""
    path = root / "README.md"
    try:
        content = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"README geometry section is unreadable: {exc}")
        return
    expected = "\n".join(_render_polygon_geometry_section(geometry)) + "\n"
    match = re.search(r"(?ms)^## Polygon geometry\n.*?(?=^## |\Z)", content)
    if match is None or match.group(0) != expected:
        errors.append("README Polygon geometry section does not match artifact-derived statistics")


def _verify_release_geographic_section(
    root: Path,
    stats: Any,
    errors: list[str],
) -> None:
    """Require release README geography values to match unique text identities."""
    path = root / "README.md"
    try:
        content = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"README geographic section is unreadable: {exc}")
        return
    expected = "\n".join(_render_geographic_section(stats)) + "\n"
    match = re.search(r"(?ms)^## Geographic distribution\n.*?(?=^## |\Z)", content)
    if match is None or match.group(0) != expected:
        errors.append(
            "README Geographic distribution section does not match the unique-text summary"
        )


def _verify_release_density_yaml(root: Path, stats: Any, errors: list[str]) -> None:
    """Require machine-readable geographic values to match the same summary."""
    path = root / "dataset.yaml"
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"dataset.yaml geographic fields are unreadable: {exc}")
        return
    expected = {
        "polygon_density_h3_resolution": stats.polygon_density_h3_resolution,
        "polygon_density_row_count": stats.polygon_density_row_count,
        "occupied_h3_cell_count": stats.occupied_h3_cell_count,
    }
    for key, value in expected.items():
        if not re.search(rf"(?m)^{re.escape(key)}: {value}$", content):
            errors.append(f"dataset.yaml {key} does not match the unique-text summary")


def _verify_release_website_text_section(root: Path, stats: Any, errors: list[str]) -> None:
    """Require an existing release card text section to match canonical counts."""
    path = root / "README.md"
    if not path.is_file():
        return
    content = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    heading = re.search(r"(?m)^## Website text(?:\n|$)", content)
    if heading is None:
        return
    expected = "\n".join(_render_website_text_section(stats)) + "\n"
    match = re.search(r"(?ms)^## Website text\n.*?(?=^## |\Z)", content)
    if match is None or match.group(0) != expected:
        errors.append("README Website text section does not match canonical text statistics")


def _verify_release_text_yaml(root: Path, stats: Any, errors: list[str]) -> None:
    """Require release YAML and README metadata to expose canonical values."""
    path = root / "dataset.yaml"
    readme = root / "README.md"
    if not path.is_file() and not readme.is_file():
        return
    expected = {
        "website_text_success_count": stats.website_text_success_count,
        "website_total_words": stats.website_total_words,
        "contact_website_text_success_count": stats.contact_website_text_success_count,
        "contact_website_total_words": stats.contact_website_total_words,
        "unique_text_identity_count": stats.polygons_with_any_text,
    }
    if path.is_file():
        try:
            _verify_release_yaml_fields(
                path.read_text(encoding="utf-8"), "dataset.yaml", expected, errors
            )
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"dataset.yaml text fields are unreadable: {exc}")
    if not readme.is_file():
        return
    try:
        readme_content = readme.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"README front matter text fields are unreadable: {exc}")
        return
    front_matter = re.match(r"\A---(?:\r?\n).*?(?:\r?\n)---(?:\r?\n|$)", readme_content, re.DOTALL)
    if front_matter is not None:
        _verify_release_yaml_fields(front_matter.group(0), "README front matter", expected, errors)
    expected_languages = "\n".join(_render_language_section(stats)) + "\n"
    language_match = re.search(
        r"(?ms)^## Languages\n.*?(?=^## |\Z)", readme_content.replace("\r\n", "\n")
    )
    if language_match is not None and language_match.group(0) != expected_languages:
        errors.append("README Languages section does not match canonical text statistics")


def _verify_release_yaml_fields(
    content: str,
    label: str,
    expected: dict[str, object],
    errors: list[str],
) -> None:
    """Compare one YAML-like document's generated text fields."""
    for key, value in expected.items():
        if not re.search(rf"(?m)^{re.escape(key)}: {value}$", content):
            errors.append(f"{label} {key} does not match canonical text statistics")


def _compare_card_file(
    path: Path,
    expected: str,
    label: str,
    errors: list[str],
) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") != expected:
        errors.append(f"{label} does not match artifact-derived statistics")


def _verify_map_artifact(
    root: Path,
    errors: list[str],
    *,
    require_readme_reference: bool = True,
) -> None:
    _verify_map_file(root / POLYGON_DENSITY_ASSET_REL_PATH, errors)
    if require_readme_reference:
        _verify_readme_map_reference(root / "README.md", errors)


def _verify_map_file(path: Path, errors: list[str]) -> None:
    """Verify that the generated map exists and has a PNG signature."""
    if not path.is_file():
        errors.append(f"missing map artifact: {POLYGON_DENSITY_ASSET_REL_PATH}")
    elif path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        errors.append("map artifact is not a valid PNG")


def _verify_readme_map_reference(path: Path, errors: list[str]) -> None:
    """Verify that the standard card links to the generated map."""
    if path.is_file() and POLYGON_DENSITY_ASSET_REL_PATH not in path.read_text(encoding="utf-8"):
        errors.append(f"README does not reference {POLYGON_DENSITY_ASSET_REL_PATH}")


def _verify_analysis_arithmetic(root: Path, errors: list[str]) -> None:
    cells = pq.read_table(root / "analysis" / "cells_global.parquet").to_pylist()
    expected_cells = {
        "cell_000_w0_c0_d0",
        "cell_001_w0_c0_d1",
        "cell_010_w0_c1_d0",
        "cell_011_w0_c1_d1",
        "cell_100_w1_c0_d0",
        "cell_101_w1_c0_d1",
        "cell_110_w1_c1_d0",
        "cell_111_w1_c1_d1",
    }
    observation_rows = [row for row in cells if row.get("level") == "observation"]
    canonical_rows = [row for row in cells if row.get("level") == "canonical"]
    _verify_observation_cells(root, observation_rows, expected_cells, errors)
    _verify_canonical_cells(canonical_rows, observation_rows, expected_cells, errors)


def _verify_observation_cells(
    root: Path,
    rows: list[dict[str, Any]],
    expected_cells: set[str],
    errors: list[str],
) -> None:
    if _verify_cell_set(rows, "observation", expected_cells, errors):
        _verify_observation_total(root, rows, errors)


def _verify_canonical_cells(
    rows: list[dict[str, Any]],
    observation_rows: list[dict[str, Any]],
    expected_cells: set[str],
    errors: list[str],
) -> None:
    if _verify_cell_set(rows, "canonical", expected_cells, errors):
        _verify_canonical_total(rows, observation_rows, errors)


def _verify_cell_set(
    rows: list[dict[str, Any]],
    level: str,
    expected_cells: set[str],
    errors: list[str],
) -> bool:
    if {row.get("cell") for row in rows} != expected_cells:
        errors.append(f"{level} analysis does not contain exactly eight cells")
        return False
    return True


def _verify_observation_total(
    root: Path,
    rows: list[dict[str, Any]],
    errors: list[str],
) -> None:
    total = sum(int(row["row_count"]) for row in rows)
    manifest = json.loads((root / "manifests" / "sources.json").read_text(encoding="utf-8"))
    expected_total = sum(int(entry["observation_row_count"]) for entry in manifest)
    if total != expected_total:
        errors.append(f"observation cell total mismatch: {total} != {expected_total}")


def _verify_canonical_total(
    canonical_rows: list[dict[str, Any]],
    observation_rows: list[dict[str, Any]],
    errors: list[str],
) -> None:
    total = sum(int(row["row_count"]) for row in canonical_rows)
    observation_total = sum(int(row["row_count"]) for row in observation_rows)
    if total > observation_total:
        errors.append("canonical cell total exceeds observation total")
