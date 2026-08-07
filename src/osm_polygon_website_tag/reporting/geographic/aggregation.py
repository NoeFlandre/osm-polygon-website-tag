"""Single source of truth for H3 polygon-density aggregation."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from osm_polygon_website_tag.reporting.geographic.h3_geometry import assign_h3_cell
from osm_polygon_website_tag.reporting.geographic.inputs import iter_lat_lon_runs
from osm_polygon_website_tag.reporting.geographic.models import (
    DEFAULT_H3_RESOLUTION,
    GeographicMapError,
    PolygonDensitySummary,
)


def compute_polygon_density_summary(
    run_dir: Path | str,
    *,
    h3_resolution: int = DEFAULT_H3_RESOLUTION,
    source_names: Collection[str] | None = None,
    extracted_text_only: bool = False,
) -> PolygonDensitySummary:
    """Aggregate selected public polygon centroids into deterministic H3 counts.

    Set ``extracted_text_only`` to count only rows with a successful
    ``website`` or ``contact:website`` text extraction.
    """
    counts: dict[str, int] = {}
    row_count = 0
    for path, row_index, lat, lon in iter_lat_lon_runs(
        run_dir,
        source_names=source_names,
        extracted_text_only=extracted_text_only,
    ):
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
    )
