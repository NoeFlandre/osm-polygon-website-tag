"""Single source of truth for H3 polygon-density aggregation."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from osm_polygon_website_tag.reporting.geographic.h3_geometry import assign_h3_cell
from osm_polygon_website_tag.reporting.geographic.inputs import (
    iter_lat_lon_runs,
    iter_unique_text_lat_lon_runs,
)
from osm_polygon_website_tag.reporting.geographic.models import (
    DEFAULT_H3_RESOLUTION,
    AggregationMode,
    GeographicMapError,
    PolygonDensitySummary,
)


def compute_polygon_density_summary(
    run_dir: Path | str,
    *,
    h3_resolution: int = DEFAULT_H3_RESOLUTION,
    source_names: Collection[str] | None = None,
    extracted_text_only: bool = False,
    aggregation_mode: AggregationMode | None = None,
) -> PolygonDensitySummary:
    """Aggregate selected polygon centroids into deterministic H3 counts.

    ``regional_rows`` counts source-level observations. ``global_unique_text``
    counts one canonical ``(osm_type, osm_id)`` per qualifying identity.
    ``extracted_text_only`` remains a compatibility alias for the global mode.
    """
    mode = _resolve_mode(extracted_text_only, aggregation_mode)
    counts: dict[str, int] = {}
    row_count = 0
    iterator = (
        iter_unique_text_lat_lon_runs(run_dir, source_names=source_names)
        if mode == "global_unique_text"
        else iter_lat_lon_runs(run_dir, source_names=source_names)
    )
    for path, row_index, lat, lon in iterator:
        try:
            cell = assign_h3_cell(lat, lon, resolution=h3_resolution)
        except GeographicMapError as exc:
            raise GeographicMapError(f"{path.name} row {row_index}: {exc}") from exc
        counts[cell] = counts.get(cell, 0) + 1
        row_count += 1
    cells = tuple(sorted(counts.items()))
    return PolygonDensitySummary(
        h3_resolution=h3_resolution,
        polygon_row_count=row_count,
        occupied_cell_count=len(cells),
        cells=cells,
        extracted_text_only=mode == "global_unique_text",
        aggregation_mode=mode,
    )


def _resolve_mode(
    extracted_text_only: bool,
    aggregation_mode: AggregationMode | None,
) -> AggregationMode:
    """Resolve the old boolean and the explicit public mode."""
    if aggregation_mode is None:
        return "global_unique_text" if extracted_text_only else "regional_rows"
    if aggregation_mode == "regional_rows" and extracted_text_only:
        raise ValueError("extracted_text_only conflicts with regional_rows")
    return aggregation_mode
