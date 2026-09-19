"""Promote release-derived card artifacts as one atomic bundle.

This module owns release-time recomputation and promotion. The reporting card
module remains focused on fresh builds and compatibility updates.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from osm_polygon_website_tag.reporting.card import (
    atomic_promote_bundle,
    render_card_bundle,
)
from osm_polygon_website_tag.reporting.card_metadata import (
    _FRONT_MATTER,
    _merge_yaml_custom_metadata,
    _render_yaml_front_matter,
    _update_readme_front_matter,
    _update_release_yaml,
    readme_preserved_sha256_bytes,
    yaml_custom_sha256_bytes,
)
from osm_polygon_website_tag.reporting.card_patching import (
    _update_geographic_section,
    _update_geometry_section,
    _update_language_section,
    _update_website_text_section,
)
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
from osm_polygon_website_tag.reporting.geographic.aggregation import (
    compute_polygon_density_summary,
)
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary
from osm_polygon_website_tag.reporting.geographic.polygon_density import build_polygon_density_map
from osm_polygon_website_tag.reporting.geometry_stats import (
    GEOMETRY_STATS_FILENAME,
    GeometryStats,
    compute_geometry_stats,
    render_geometry_stats,
)
from osm_polygon_website_tag.reporting.text_population import compute_text_population_summary


def refresh_card_for_release(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
    expected_readme_custom_sha256: str | None = None,
    expected_dataset_custom_sha256: str | None = None,
    expected_readme_preserved_sha256: str | None = None,
) -> Path:
    """Refresh every geography-bearing release artifact from one text summary.

    Existing card sections outside the derived geography and geometry blocks
    remain byte-for-byte intact. The map, README, YAML density fields, and
    geometry report are promoted together so a release cannot retain stale
    geographic metadata.
    """
    root = Path(run_dir)
    readme = root / "README.md"
    if not readme.is_file():
        yaml_path = root / "dataset.yaml"
        original_yaml = yaml_path.read_bytes() if yaml_path.is_file() else None
        bundle = render_card_bundle(
            root,
            source_names=source_names,
            _yaml_source=original_yaml,
        )
        _validate_trusted_card_identity(
            bundle.readme,
            bundle.dataset_yaml,
            expected_readme_custom_sha256=expected_readme_custom_sha256,
            expected_dataset_custom_sha256=expected_dataset_custom_sha256,
            expected_readme_preserved_sha256=expected_readme_preserved_sha256,
        )
        promote_release_card_artifacts(
            root,
            source_names=source_names,
            summary=bundle.summary,
            geometry=bundle.geometry,
            readme=readme,
            original_readme=None,
            updated_readme=bundle.readme,
            yaml_path=yaml_path,
            original_yaml=original_yaml,
            updated_yaml=bundle.dataset_yaml,
        )
        return readme

    text_population = compute_text_population_summary(root, source_names=source_names)
    summary = compute_polygon_density_summary(
        root,
        source_names=source_names,
        aggregation_mode="global_unique_text",
    )
    stats = compute_card_stats(
        root,
        summary=summary,
        text_population=text_population,
        source_names=source_names,
    )
    geometry = compute_geometry_stats(
        root,
        source_names=source_names,
        text_population=text_population,
    )
    original_readme = readme.read_bytes()
    updated_readme = _update_readme_front_matter(original_readme, stats)
    updated_readme = _update_website_text_section(updated_readme, stats)
    updated_readme = _update_language_section(updated_readme, stats)
    updated_readme = _update_geographic_section(
        _update_geometry_section(updated_readme, geometry), stats
    )
    yaml_path = root / "dataset.yaml"
    original_yaml = yaml_path.read_bytes() if yaml_path.is_file() else None
    updated_yaml = (
        _update_release_yaml(original_yaml, stats)
        if original_yaml is not None
        else _merge_yaml_custom_metadata(
            _render_yaml_front_matter(stats).encode("utf-8"),
            _readme_front_matter(original_readme),
        )
    )
    _validate_trusted_card_identity(
        updated_readme,
        updated_yaml,
        expected_readme_custom_sha256=expected_readme_custom_sha256,
        expected_dataset_custom_sha256=expected_dataset_custom_sha256,
        expected_readme_preserved_sha256=expected_readme_preserved_sha256,
    )
    promote_release_card_artifacts(
        root,
        source_names=source_names,
        summary=summary,
        geometry=geometry,
        readme=readme,
        original_readme=original_readme,
        updated_readme=updated_readme,
        yaml_path=yaml_path,
        original_yaml=original_yaml,
        updated_yaml=updated_yaml,
    )
    return readme


def promote_release_card_artifacts(
    root: Path,
    *,
    source_names: Collection[str] | None,
    summary: PolygonDensitySummary,
    geometry: GeometryStats,
    readme: Path,
    original_readme: bytes | None,
    updated_readme: bytes,
    yaml_path: Path,
    original_yaml: bytes | None,
    updated_yaml: bytes | None,
) -> None:
    """Render and atomically promote the release-derived card artifacts."""
    staged_readme = root / ".README.md.release.building"
    staged_yaml = root / ".dataset.yaml.release.building"
    staged_stats = root / ".stats.json.release.building"
    staged_map = root / ".assets" / "geographic_polygon_density.png.release.building"
    staged_map.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_polygon_density_map(
            root,
            summary=summary,
            output_path=staged_map,
            source_names=source_names,
            aggregation_mode="global_unique_text",
        )
        promotions = _release_card_promotions(
            root,
            readme=readme,
            original_readme=original_readme,
            updated_readme=updated_readme,
            yaml_path=yaml_path,
            original_yaml=original_yaml,
            updated_yaml=updated_yaml,
            staged_readme=staged_readme,
            staged_yaml=staged_yaml,
            staged_map=staged_map,
            staged_stats=staged_stats,
            geometry=geometry,
        )
        if promotions:
            atomic_promote_bundle(promotions)
    finally:
        staged_map.unlink(missing_ok=True)
        staged_readme.unlink(missing_ok=True)
        staged_yaml.unlink(missing_ok=True)
        staged_stats.unlink(missing_ok=True)


def _release_card_promotions(
    root: Path,
    *,
    readme: Path,
    original_readme: bytes | None,
    updated_readme: bytes,
    yaml_path: Path,
    original_yaml: bytes | None,
    updated_yaml: bytes | None,
    staged_readme: Path,
    staged_yaml: Path,
    staged_map: Path,
    staged_stats: Path,
    geometry: GeometryStats,
) -> list[tuple[Path, Path]]:
    """Stage only changed release metadata plus the derived geometry report."""
    return [
        *_stage_readme_promotion(
            readme,
            original_readme,
            updated_readme,
            staged_readme,
        ),
        *_stage_yaml_promotion(
            yaml_path,
            original_yaml,
            updated_yaml,
            staged_yaml,
        ),
        *_stage_release_map(root, staged_map),
        *_stage_geometry_stats(staged_stats, root, geometry),
    ]


def _stage_readme_promotion(
    target: Path,
    original: bytes | None,
    updated: bytes,
    staged: Path,
) -> list[tuple[Path, Path]]:
    """Stage README bytes when the release has changed or rebuilt them."""
    if original is not None and updated == original:
        return []
    staged.write_bytes(updated)
    return [(staged, target)]


def _stage_yaml_promotion(
    target: Path,
    original: bytes | None,
    updated: bytes | None,
    staged: Path,
) -> list[tuple[Path, Path]]:
    """Stage dataset YAML bytes when a release artifact is available and changed."""
    if updated is None or (original is not None and updated == original):
        return []
    staged.write_bytes(updated)
    return [(staged, target)]


def _readme_front_matter(document: bytes) -> bytes:
    """Return the surviving README metadata source, without its body."""
    match = _FRONT_MATTER.match(document)
    return match.group(0) if match is not None else b""


def _validate_trusted_card_identity(
    readme: bytes,
    dataset_yaml: bytes,
    *,
    expected_readme_custom_sha256: str | None,
    expected_dataset_custom_sha256: str | None,
    expected_readme_preserved_sha256: str | None,
) -> None:
    """Validate recoverable metadata before any release artifact is promoted."""
    actual_readme_custom = yaml_custom_sha256_bytes(readme, readme=True)
    _validate_readme_custom_identity(actual_readme_custom, expected_readme_custom_sha256)
    _validate_dataset_custom_identity(dataset_yaml, expected_dataset_custom_sha256)
    _validate_readme_body_identity(readme, actual_readme_custom, expected_readme_preserved_sha256)


def _validate_readme_custom_identity(actual: str | None, expected: str | None) -> None:
    """Reject a staged README whose trusted custom YAML identity changed."""
    if expected is not None and actual != expected:
        raise ValueError("missing README custom metadata cannot be recovered")


def _validate_dataset_custom_identity(document: bytes, expected: str | None) -> None:
    """Reject a staged dataset YAML whose trusted custom identity changed."""
    if expected is not None and yaml_custom_sha256_bytes(document) != expected:
        raise ValueError("dataset YAML custom metadata cannot be recovered")


def _validate_readme_body_identity(
    document: bytes,
    custom_identity: str | None,
    expected: str | None,
) -> None:
    """Reject a staged README whose preserved body identity changed."""
    if expected is not None and custom_identity is not None:
        actual = readme_preserved_sha256_bytes(document)
        if actual != expected:
            raise ValueError("missing README body cannot be recovered")


def _stage_release_map(root: Path, staged_map: Path) -> list[tuple[Path, Path]]:
    """Return a map promotion only when the rendered bytes differ."""
    target = root / POLYGON_DENSITY_ASSET_REL_PATH
    if target.is_file() and staged_map.read_bytes() == target.read_bytes():
        return []
    return [(staged_map, target)]


def _stage_geometry_stats(
    staged: Path,
    run_dir: Path,
    geometry: GeometryStats,
) -> list[tuple[Path, Path]]:
    """Stage stats JSON only when regeneration would change its bytes."""
    target = run_dir / GEOMETRY_STATS_FILENAME
    rendered = render_geometry_stats(geometry)
    if target.is_file() and target.read_text(encoding="utf-8") == rendered:
        return []
    staged.write_text(rendered, encoding="utf-8")
    return [(staged, target)]


__all__ = ["promote_release_card_artifacts", "refresh_card_for_release"]
