"""Offline Natural Earth land backdrop shared by the H3 renderer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib.patches as patches

BUNDLED_LAND_FILENAME = "ne_110m_admin_0_countries.geojson"
BUNDLED_LAND_PATH = Path(__file__).with_name(BUNDLED_LAND_FILENAME)

OCEAN_COLOR = "#cfe2f3"
LAND_COLOR = "#e8e0d0"
LAND_EDGE_COLOR = "#b8aa90"


def draw_landmasses(axis: Any, geojson_path: Path = BUNDLED_LAND_PATH) -> None:
    """Draw bundled Natural Earth country polygons without network access."""
    if not geojson_path.is_file():
        raise FileNotFoundError(f"missing bundled land backdrop: {geojson_path}")
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    for feature in data.get("features", []):
        geometry = feature.get("geometry", {})
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "Polygon" and coordinates:
            _draw_polygon(axis, coordinates)
        elif geometry_type == "MultiPolygon" and coordinates:
            for polygon in coordinates:
                _draw_polygon(axis, polygon)


def _draw_polygon(axis: Any, rings: list[list[list[float]]]) -> None:
    if not rings:
        return
    axis.add_patch(
        patches.Polygon(
            rings[0],
            closed=True,
            facecolor=LAND_COLOR,
            edgecolor=LAND_EDGE_COLOR,
            linewidth=0.3,
            zorder=1,
        )
    )
    for hole in rings[1:]:
        axis.add_patch(
            patches.Polygon(
                hole,
                closed=True,
                facecolor=OCEAN_COLOR,
                edgecolor=LAND_EDGE_COLOR,
                linewidth=0.3,
                zorder=2,
            )
        )


__all__ = ["BUNDLED_LAND_PATH", "draw_landmasses"]
