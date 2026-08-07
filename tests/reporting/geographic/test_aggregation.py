"""RED tests for deterministic H3 polygon-density aggregation."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.reporting.geographic import inputs
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


def _write_text_coords(
    path: Path,
    rows: list[tuple[float, float, str, str]],
) -> None:
    pq.write_table(
        pa.table(
            {
                "lat": [lat for lat, _lon, _website, _contact in rows],
                "lon": [lon for _lat, lon, _website, _contact in rows],
                "website_text_status": [website for _lat, _lon, website, _contact in rows],
                "contact_website_text_status": [contact for _lat, _lon, _website, contact in rows],
            }
        ),
        path,
    )


def test_iter_lat_lon_runs_uses_arrow_buffers_without_row_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinate input should not materialize a Python row list per batch."""
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    path = polygons / "source.parquet"
    path.touch()

    class FakeBatch:
        num_rows = 2

        class FakeArray:
            def __init__(self, values: list[float]) -> None:
                self._array = pa.array(values, type=pa.float64())

            def to_pylist(self) -> list[float]:
                raise AssertionError("coordinate input must not materialize Python lists")

            def to_numpy(self, *, zero_copy_only: bool) -> object:
                return self._array.to_numpy(zero_copy_only=zero_copy_only)

            def is_null(self) -> pa.Array:
                return self._array.is_null()

        def column(self, name: str) -> FakeArray:
            values = {
                "lat": [48.85, 48.86],
                "lon": [2.35, 2.36],
            }
            return self.FakeArray(values[name])

    class FakeParquet:
        schema_arrow = pa.schema([("lat", pa.float64()), ("lon", pa.float64())])

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield FakeBatch()

    monkeypatch.setattr(inputs.pq, "ParquetFile", lambda _path: FakeParquet())

    runs = list(inputs.iter_lat_lon_runs(tmp_path))

    assert [(row_index, lat, lon) for _path, row_index, lat, lon in runs] == [
        (0, 48.85, 2.35),
        (1, 48.86, 2.36),
    ]


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


def test_summary_can_filter_to_polygons_with_extracted_text(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    _write_text_coords(
        polygons / "source.parquet",
        [
            (48.85, 2.35, "success", "absent"),
            (40.7, -74.0, "empty", "success"),
            (35.68, 139.69, "fetch_error", "absent"),
        ],
    )

    summary = compute_polygon_density_summary(tmp_path, extracted_text_only=True)

    assert summary.polygon_row_count == 2
    assert sum(count for _cell, count in summary.cells) == 2


def test_text_only_summary_excludes_shards_without_text_status_columns(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    _write_coords(polygons / "legacy.parquet", [(48.85, 2.35)])
    _write_text_coords(polygons / "current.parquet", [(40.7, -74.0, "success", "absent")])

    summary = compute_polygon_density_summary(tmp_path, extracted_text_only=True)

    assert summary.polygon_row_count == 1


@pytest.mark.parametrize("lat, lon", [(91.0, 0.0), (0.0, 181.0), (float("nan"), 0.0)])
def test_summary_rejects_invalid_coordinates(tmp_path: Path, lat: float, lon: float) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    _write_coords(polygons / "a.parquet", [(lat, lon)])

    with pytest.raises(ValueError, match=r"a\.parquet row 0"):
        compute_polygon_density_summary(tmp_path)


def test_summary_rejects_null_coordinates(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    pq.write_table(
        pa.table({"lat": pa.array([None], type=pa.float64()), "lon": pa.array([2.35])}),
        polygons / "a.parquet",
    )

    with pytest.raises(ValueError, match=r"null coordinate in a\.parquet row 0"):
        compute_polygon_density_summary(tmp_path)


def test_summary_empty_run_is_zero(tmp_path: Path) -> None:
    (tmp_path / "polygons").mkdir()

    summary = compute_polygon_density_summary(tmp_path)

    assert summary.polygon_row_count == 0
    assert summary.occupied_cell_count == 0
    assert summary.cells == ()
