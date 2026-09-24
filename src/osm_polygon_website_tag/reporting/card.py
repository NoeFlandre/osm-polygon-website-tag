"""Build and update the publishable dataset card bundle.

This module owns the small orchestration layer for fresh card builds and
legacy geometry updates. Markdown rendering, YAML metadata, byte-preserving
patches, and release promotion live in dedicated modules.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
)
from osm_polygon_website_tag.reporting.card_metadata import (
    _merge_yaml_custom_metadata,
    _render_yaml_front_matter,
    readme_preserved_sha256,
    readme_preserved_sha256_bytes,
    yaml_custom_sha256,
    yaml_custom_sha256_bytes,
)
from osm_polygon_website_tag.reporting.card_patching import update_geometry_section
from osm_polygon_website_tag.reporting.card_rendering import render_markdown
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
from osm_polygon_website_tag.reporting.text_population import (
    TextPopulationSummary,
    compute_text_population_summary,
)
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle

CARD_CONTRACT_VERSION = 2


@dataclass(frozen=True)
class CardBundle:
    """Purely rendered card metadata and the summaries that produced it."""

    readme: bytes
    dataset_yaml: bytes
    summary: PolygonDensitySummary
    text_population: TextPopulationSummary
    geometry: GeometryStats


def render_card_bundle(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
    _yaml_source: bytes | None = None,
) -> CardBundle:
    """Render card metadata without touching the filesystem outputs."""
    root = Path(run_dir)
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
    body = render_markdown(
        stats,
        geometry=geometry,
        schema=_public_schema_for_card(root, source_names),
    )
    front_matter = _render_yaml_front_matter(stats)
    if _yaml_source is not None:
        front_matter = _merge_yaml_custom_metadata(front_matter.encode(), _yaml_source).decode()
    return CardBundle(
        readme=(front_matter + "\n" + body).encode(),
        dataset_yaml=front_matter.encode(),
        summary=summary,
        text_population=text_population,
        geometry=geometry,
    )


def build_card(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
    _yaml_source: bytes | None = None,
) -> Path:
    """Build or rebuild the README card and its derived metadata."""
    root = Path(run_dir)
    bundle = render_card_bundle(root, source_names=source_names, _yaml_source=_yaml_source)
    readme_path = root / "README.md"
    yaml_path = root / "dataset.yaml"
    staged_readme = root / ".README.md.building"
    staged_yaml = root / ".dataset.yaml.building"
    staged_stats = root / ".stats.json.building"
    staged_map = root / ".assets" / "geographic_polygon_density.png.building"
    staged_map.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_polygon_density_map(
            root,
            summary=bundle.summary,
            output_path=staged_map,
            source_names=source_names,
            aggregation_mode="global_unique_text",
        )
        staged_readme.write_bytes(bundle.readme)
        staged_yaml.write_bytes(bundle.dataset_yaml)
        promotions = [
            (staged_map, root / POLYGON_DENSITY_ASSET_REL_PATH),
            (staged_readme, readme_path),
            (staged_yaml, yaml_path),
        ]
        promotions.extend(_staged_geometry_stats(staged_stats, root, bundle.geometry))
        atomic_promote_bundle(promotions)
    finally:
        staged_map.unlink(missing_ok=True)
        staged_readme.unlink(missing_ok=True)
        staged_yaml.unlink(missing_ok=True)
        staged_stats.unlink(missing_ok=True)
    return readme_path


def update_card_with_geometry(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> Path:
    """Add or replace the geometry section while preserving the card bundle."""
    root = Path(run_dir)
    readme = root / "README.md"
    if not readme.is_file():
        return build_card(root, source_names=source_names)

    geometry = compute_geometry_stats(root, source_names=source_names)
    original = readme.read_bytes()
    updated = update_geometry_section(original, geometry)
    staged_readme = root / ".README.md.geometry.building"
    staged_stats = root / ".stats.json.geometry.building"
    promotions: list[tuple[Path, Path]] = []
    try:
        if updated != original:
            staged_readme.write_bytes(updated)
            promotions.append((staged_readme, readme))
        promotions.extend(_staged_geometry_stats(staged_stats, root, geometry))
        if promotions:
            atomic_promote_bundle(promotions)
    finally:
        staged_readme.unlink(missing_ok=True)
        staged_stats.unlink(missing_ok=True)
    return readme


def _public_schema_for_card(
    run_dir: Path, source_names: Collection[str] | None = None
) -> pa.Schema:
    """Return the richest contract carried by the selected public artifacts."""
    paths = _selected_public_paths(run_dir, source_names)
    if _has_schema(paths, POLYGON_PUBLIC_SCHEMA_V1_5):
        return POLYGON_PUBLIC_SCHEMA_V1_5
    if _has_schema(paths, POLYGON_PUBLIC_SCHEMA_V1_4):
        return POLYGON_PUBLIC_SCHEMA_V1_4
    return POLYGON_PUBLIC_SCHEMA


def _selected_public_paths(
    run_dir: Path,
    source_names: Collection[str] | None,
) -> list[Path]:
    """Select public Parquet shards included in a card."""
    paths = sorted((run_dir / "polygons").glob("*.parquet"))
    if source_names is not None:
        selected = {
            f"{source_name.removesuffix('.osm.pbf')}.parquet" for source_name in source_names
        }
        paths = [path for path in paths if path.name in selected]
    return paths


def _has_schema(paths: Collection[Path], schema: pa.Schema) -> bool:
    """Return whether any selected shard carries an exact public contract."""
    return any(pq.read_schema(path).equals(schema, check_metadata=True) for path in paths)


def _staged_geometry_stats(
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


__all__ = [
    "CARD_CONTRACT_VERSION",
    "CardBundle",
    "build_card",
    "readme_preserved_sha256",
    "readme_preserved_sha256_bytes",
    "render_card_bundle",
    "update_card_with_geometry",
    "yaml_custom_sha256",
    "yaml_custom_sha256_bytes",
]
