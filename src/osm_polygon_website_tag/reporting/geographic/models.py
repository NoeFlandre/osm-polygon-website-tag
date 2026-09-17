"""Typed values shared by geographic aggregation and rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_H3_RESOLUTION = 3
MAP_CONTRACT_VERSION = 2
AggregationMode = Literal["regional_rows", "global_unique_text"]


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
    aggregation_mode: AggregationMode | None = None

    def __post_init__(self) -> None:
        """Resolve the legacy boolean and explicit aggregation mode together."""
        mode = self.aggregation_mode
        if mode is None:
            mode = "global_unique_text" if self.extracted_text_only else "regional_rows"
        if mode not in ("regional_rows", "global_unique_text"):
            raise ValueError(f"unsupported aggregation mode: {mode}")
        if self.extracted_text_only != (mode == "global_unique_text"):
            object.__setattr__(self, "extracted_text_only", mode == "global_unique_text")
        object.__setattr__(self, "aggregation_mode", mode)


@dataclass(frozen=True)
class PolygonDensityRenderResult:
    """Result of rendering a polygon-density map."""

    output_path: Path
    h3_resolution: int
    polygon_row_count: int
    occupied_cell_count: int
    caption: str
    aggregation_mode: AggregationMode = "regional_rows"
