"""Headless deterministic Matplotlib rendering for the H3 density map."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from matplotlib import colors, patches
from matplotlib import pyplot as plt

from osm_polygon_website_tag.reporting.geographic.h3_geometry import cell_boundary_rings
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary


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
    fig, axis = plt.subplots(figsize=(16, 8), dpi=100)
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
        if summary.cells:
            maximum = max(count for _cell, count in summary.cells)
            norm = colors.LogNorm(vmin=0.5, vmax=max(1.0, float(maximum)))
            cmap = plt.get_cmap("magma")
            for cell, count in summary.cells:
                for ring in cell_boundary_rings(cell):
                    polygon = patches.Polygon(
                        ring,
                        closed=True,
                        facecolor=cmap(norm(count)),
                        edgecolor="#333333",
                        linewidth=0.25,
                        alpha=0.95,
                    )
                    axis.add_patch(polygon)
            scalar = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
            scalar.set_array([])
            fig.colorbar(scalar, ax=axis, label="Polygons per H3 cell (log scale)")
        else:
            axis.text(
                0.5, 0.5, "No public polygon centroids", transform=axis.transAxes, ha="center"
            )
        caption = (
            f"H3 resolution {summary.h3_resolution}; {summary.occupied_cell_count:,} occupied "
            f"cells across {summary.polygon_row_count:,} polygon centroids; logarithmic scale. "
            "No basemap is rendered."
        )
        fig.text(0.5, 0.01, caption, ha="center", fontsize=8)
        atomic_save_png(fig, output_path)
        return caption
    finally:
        plt.close(fig)
