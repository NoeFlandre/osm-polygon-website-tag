"""Build the public H3 polygon-density map artifact."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_website_tag.reporting.geographic.aggregation import (
    compute_polygon_density_summary,
)
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geographic.models import (
    DEFAULT_H3_RESOLUTION,
    PolygonDensityRenderResult,
    PolygonDensitySummary,
)
from osm_polygon_website_tag.reporting.geographic.rendering import render_polygon_density


def build_polygon_density_map(
    run_dir: Path | str,
    *,
    summary: PolygonDensitySummary | None = None,
    output_path: Path | None = None,
    h3_resolution: int = DEFAULT_H3_RESOLUTION,
) -> PolygonDensityRenderResult:
    """Aggregate and render the map, reusing a supplied summary when present."""
    root = Path(run_dir)
    resolved_summary = summary or compute_polygon_density_summary(root, h3_resolution=h3_resolution)
    destination = output_path or root / POLYGON_DENSITY_ASSET_REL_PATH
    caption = render_polygon_density(resolved_summary, destination)
    return PolygonDensityRenderResult(
        output_path=destination,
        h3_resolution=resolved_summary.h3_resolution,
        polygon_row_count=resolved_summary.polygon_row_count,
        occupied_cell_count=resolved_summary.occupied_cell_count,
        caption=caption,
    )
