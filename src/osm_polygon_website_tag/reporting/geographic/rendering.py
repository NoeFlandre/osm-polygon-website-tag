"""Headless deterministic Matplotlib rendering for the H3 density map."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.reporting.geographic.basemap import (
    BUNDLED_LAND_PATH,
    draw_landmasses,
)
from osm_polygon_website_tag.reporting.geographic.h3_geometry import cell_boundary_rings
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary

_LAZY_COMPONENT_INDEX = {"colors": 0, "patches": 1, "plt": 2}


def _matplotlib_components() -> tuple[Any, Any, Any]:
    """Load Matplotlib only when a map is actually rendered.

    Card verification imports this module even when it only checks text and
    YAML. Deferring the GUI-heavy import avoids a system font scan in every
    isolated test and mutation process while keeping the module attributes
    available for renderer tests and callers that patch them.
    """
    pyplot = globals().get("plt")
    colors_module = globals().get("colors")
    patches_module = globals().get("patches")
    if pyplot is None or colors_module is None or patches_module is None:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import colors as colors_module
        from matplotlib import patches as patches_module
        from matplotlib import pyplot as pyplot

        globals().update(
            colors=colors_module,
            patches=patches_module,
            plt=pyplot,
        )
    return colors_module, patches_module, pyplot


def __getattr__(name: str) -> Any:
    """Expose lazily loaded Matplotlib modules for compatibility and tests."""
    try:
        component_index = _LAZY_COMPONENT_INDEX[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    return _matplotlib_components()[component_index]


def atomic_save_png(fig, output_path: Path) -> None:
    """Save a figure through a same-directory temporary file and replace."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        fig.savefig(
            temporary,
            format="png",
            dpi=100,
            facecolor="white",
            metadata={"Software": "osm-polygon-website-tag"},
        )
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)


def render_polygon_density(summary: PolygonDensitySummary, output_path: Path) -> str:
    """Render and atomically save a world-coordinate H3 density map."""
    colors_module, patches_module, pyplot = _matplotlib_components()
    fig, axis = pyplot.subplots(figsize=(16, 8), dpi=100)
    try:
        axis.set_facecolor("#cfe2f3")
        axis.set_xlim(-180, 180)
        axis.set_ylim(-90, 90)
        axis.set_xticks(range(-180, 181, 30))
        axis.set_yticks(range(-90, 91, 30))
        axis.set_xlabel("Longitude")
        axis.set_ylabel("Latitude")
        axis.set_title("OSM polygon density by H3 cell (log scale)")
        axis.grid(True, color="white", linewidth=0.3, alpha=0.5)
        axis.set_aspect("equal", adjustable="box")
        draw_landmasses(axis, BUNDLED_LAND_PATH)
        if summary.cells:
            maximum = max(count for _cell, count in summary.cells)
            norm = colors_module.LogNorm(vmin=0.5, vmax=max(1.0, float(maximum)))
            cmap = pyplot.get_cmap("magma")
            for cell, count in summary.cells:
                for ring in cell_boundary_rings(cell):
                    polygon = patches_module.Polygon(
                        ring,
                        closed=True,
                        facecolor=cmap(norm(count)),
                        edgecolor="#333333",
                        linewidth=0.25,
                        alpha=0.95,
                    )
                    axis.add_patch(polygon)
            scalar = pyplot.cm.ScalarMappable(norm=norm, cmap=cmap)
            scalar.set_array([])
            fig.colorbar(scalar, ax=axis, label="Polygons per H3 cell (log scale)")
        else:
            axis.text(
                0.5,
                0.5,
                _empty_scope_label(summary),
                transform=axis.transAxes,
                ha="center",
            )
        scope_label = _scope_label(summary)
        caption = (
            f"H3 resolution {summary.h3_resolution}; {summary.occupied_cell_count:,} occupied "
            f"cells across {summary.polygon_row_count:,} {scope_label}; "
            "logarithmic scale. "
            "Natural Earth 1:110m land backdrop."
        )
        fig.text(0.5, 0.01, caption, ha="center", fontsize=8)
        atomic_save_png(fig, output_path)
        return caption
    finally:
        pyplot.close(fig)


def _scope_label(summary: PolygonDensitySummary) -> str:
    """Describe the aggregation scope without overstating regional rows."""
    if summary.extracted_text_only:
        return (
            "unique polygons with extracted text (successful non-empty website or contact:website "
            "text; regional overlap duplicates removed globally)"
        )
    return "regional rows/centroids (regional polygon rows/centroids)"


def _empty_scope_label(summary: PolygonDensitySummary) -> str:
    """Describe an empty map using the same scope as its caption."""
    if summary.extracted_text_only:
        return (
            "No unique polygons with extracted text; regional overlap duplicates removed globally"
        )
    return "No regional polygon rows/centroids"
