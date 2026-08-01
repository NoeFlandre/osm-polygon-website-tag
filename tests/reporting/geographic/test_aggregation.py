"""RED tests for deterministic H3 polygon-density aggregation."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.reporting.geographic.aggregation import (
    compute_polygon_density_summary,
)


def _write_coords(path: Path, rows: list[tuple[float, float]]) -> None:
    pq.write_table(
        pa.table(
            {
                "lat": [lat for lat, _lon in rows],
                "lon": [lon for _lat, lon in rows],
                "ignored": list(range(len(rows))),
            }
        ),
        path,
    )


def test_summary_counts_rows_and_sorts_h3_cells(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    _write_coords(polygons / "b.parquet", [(48.85, 2.35), (48.86, 2.36)])
    _write_coords(polygons / "a.parquet", [(40.7, -74.0)])

    summary = compute_polygon_density_summary(tmp_path)

    assert summary.polygon_row_count == 3
    assert summary.occupied_cell_count == 2
    assert list(summary.cells) == sorted(summary.cells)
    assert sum(count for _cell, count in summary.cells) == 3
    assert summary.h3_resolution == 3


def test_summary_can_scope_to_uploaded_sources(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    _write_coords(polygons / "uploaded.parquet", [(48.85, 2.35)])
    _write_coords(polygons / "local-only.parquet", [(40.7, -74.0), (40.71, -74.01)])

    summary = compute_polygon_density_summary(
        tmp_path,
        source_names={"uploaded.osm.pbf"},
    )

    assert summary.polygon_row_count == 1


@pytest.mark.parametrize("lat, lon", [(91.0, 0.0), (0.0, 181.0), (float("nan"), 0.0)])
def test_summary_rejects_invalid_coordinates(tmp_path: Path, lat: float, lon: float) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    _write_coords(polygons / "a.parquet", [(lat, lon)])

    with pytest.raises(ValueError, match=r"a\.parquet row 0"):
        compute_polygon_density_summary(tmp_path)


def test_summary_empty_run_is_zero(tmp_path: Path) -> None:
    (tmp_path / "polygons").mkdir()

    summary = compute_polygon_density_summary(tmp_path)

    assert summary.polygon_row_count == 0
    assert summary.occupied_cell_count == 0
    assert summary.cells == ()
