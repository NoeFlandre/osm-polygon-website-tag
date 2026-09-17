"""Typed values shared by geographic aggregation and rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_H3_RESOLUTION = 3
MAP_CONTRACT_VERSION = 2


class GeographicMapError(ValueError):
    """Raised when a public polygon coordinate cannot be mapped safely."""


@dataclass(frozen=True)
class PolygonDensitySummary:
    """Deterministic H3 counts with an explicit aggregation scope."""

    h3_resolution: int
    polygon_row_count: int
    occupied_cell_count: int
    cells: tuple[tuple[str, int], ...]
    extracted_text_only: bool = False


@dataclass(frozen=True)
class PolygonDensityRenderResult:
    """Result of rendering a polygon-density map."""

    output_path: Path
    h3_resolution: int
    polygon_row_count: int
    occupied_cell_count: int
    caption: str
