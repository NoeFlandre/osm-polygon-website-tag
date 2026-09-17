"""RED tests for deterministic H3 polygon-density aggregation."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.reporting.geographic.aggregation as aggregation_module
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
                "osm_type": ["way"] * len(rows),
                "osm_id": list(range(1, len(rows) + 1)),
                "website_text": pa.array(
                    [
                        "website text"
                        if website == "success"
                        else ""
                        if website == "empty"
                        else None
                        for _lat, _lon, website, _contact in rows
                    ],
                    type=pa.string(),
                ),
                "website_text_status": [website for _lat, _lon, website, _contact in rows],
                "contact_website_text": pa.array(
                    [
                        "contact text"
                        if contact == "success"
                        else ""
                        if contact == "empty"
                        else None
                        for _lat, _lon, _website, contact in rows
                    ],
                    type=pa.string(),
                ),
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
    assert summary.extracted_text_only is False


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
    assert summary.extracted_text_only is True


def test_summary_exposes_explicit_global_and_regional_modes(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    _write_text_coords(polygons / "source.parquet", [(48.85, 2.35, "success", "absent")])

    global_summary = compute_polygon_density_summary(
        tmp_path,
        aggregation_mode="global_unique_text",
    )
    regional_summary = compute_polygon_density_summary(
        tmp_path,
        aggregation_mode="regional_rows",
    )

    assert global_summary.aggregation_mode == "global_unique_text"
    assert regional_summary.aggregation_mode == "regional_rows"
    assert global_summary.extracted_text_only is True
    assert regional_summary.extracted_text_only is False

    with pytest.raises(ValueError, match="conflicts"):
        compute_polygon_density_summary(
            tmp_path,
            extracted_text_only=True,
            aggregation_mode="regional_rows",
        )


def test_text_summary_deduplicates_osm_identity_and_requires_non_empty_text(
    tmp_path: Path,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    schema = pa.schema(
        [
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("osm_type", pa.string()),
            ("osm_id", pa.int64()),
            ("website_text", pa.string()),
            ("website_text_status", pa.string()),
            ("contact_website_text", pa.string()),
            ("contact_website_text_status", pa.string()),
        ]
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "lat": 48.85,
                    "lon": 2.35,
                    "osm_type": "way",
                    "osm_id": 42,
                    "website_text": "same polygon",
                    "website_text_status": "success",
                    "contact_website_text": None,
                    "contact_website_text_status": "absent",
                },
                {
                    "lat": 40.7,
                    "lon": -74.0,
                    "osm_type": "way",
                    "osm_id": 42,
                    "website_text": "same polygon copy",
                    "website_text_status": "success",
                    "contact_website_text": None,
                    "contact_website_text_status": "absent",
                },
                {
                    "lat": 35.68,
                    "lon": 139.69,
                    "osm_type": "relation",
                    "osm_id": 42,
                    "website_text": " ",
                    "website_text_status": "success",
                    "contact_website_text": None,
                    "contact_website_text_status": "absent",
                },
                {
                    "lat": 35.7,
                    "lon": 139.7,
                    "osm_type": "way",
                    "osm_id": 43,
                    "website_text": "failed",
                    "website_text_status": "fetch_error",
                    "contact_website_text": None,
                    "contact_website_text_status": "absent",
                },
            ],
            schema=schema,
        ),
        polygons / "a.parquet",
    )

    summary = compute_polygon_density_summary(tmp_path, extracted_text_only=True)

    assert summary.polygon_row_count == 1
    assert sum(count for _cell, count in summary.cells) == 1
    assert summary.extracted_text_only is True


def test_text_summary_deduplicates_same_identity_across_regional_shards(
    tmp_path: Path,
) -> None:
    regional_run = tmp_path / "regional"
    regional_polygons = regional_run / "polygons"
    regional_polygons.mkdir(parents=True)
    regional_schema = pa.schema(
        [
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("osm_type", pa.string()),
            ("osm_id", pa.int64()),
            ("website_text", pa.string()),
            ("website_text_status", pa.string()),
            ("contact_website_text", pa.string()),
            ("contact_website_text_status", pa.string()),
        ]
    )
    for filename, lat, lon, text in (
        ("bretagne.parquet", 48.85, 2.35, "  first regional copy  "),
        ("monaco.parquet", 40.7, -74.0, "second regional copy"),
    ):
        pq.write_table(
            pa.Table.from_pylist(
                [
                    {
                        "lat": lat,
                        "lon": lon,
                        "osm_type": "way",
                        "osm_id": 42,
                        "website_text": text,
                        "website_text_status": "success",
                        "contact_website_text": None,
                        "contact_website_text_status": "absent",
                    }
                ],
                schema=regional_schema,
            ),
            regional_polygons / filename,
        )

    (regional_run / "analysis_observations").mkdir()
    canonical_run = tmp_path / "canonical"
    canonical_run.mkdir()
    (canonical_run / "analysis_observations").symlink_to(
        regional_run / "analysis_observations",
        target_is_directory=True,
    )

    summary = compute_polygon_density_summary(canonical_run, extracted_text_only=True)

    assert summary.polygon_row_count == 1
    assert sum(count for _cell, count in summary.cells) == 1


def test_text_summary_uses_regional_copies_for_canonical_runs(tmp_path: Path) -> None:
    regional_run = tmp_path / "regional"
    (regional_run / "polygons").mkdir(parents=True)
    _write_text_coords(
        regional_run / "polygons" / "regional.parquet",
        [(48.85, 2.35, "success", "absent"), (40.7, -74.0, "success", "absent")],
    )

    canonical_run = tmp_path / "canonical"
    (canonical_run / "polygons").mkdir(parents=True)
    (canonical_run / "analysis_observations").symlink_to(
        regional_run / "analysis_observations",
        target_is_directory=True,
    )
    (regional_run / "analysis_observations").mkdir()
    _write_text_coords(
        canonical_run / "polygons" / "canonical.parquet",
        [(35.7, 139.7, "success", "absent")],
    )

    summary = compute_polygon_density_summary(canonical_run, extracted_text_only=True)

    assert summary.polygon_row_count == 2


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


def test_summary_selects_the_requested_input_iterator_and_binds_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standard = [(Path("standard.parquet"), 0, 1.0, 2.0)]
    text_only = [(Path("text.parquet"), 4, 3.0, 4.0), (Path("text.parquet"), 5, 3.0, 4.0)]
    calls: list[tuple[str, object, object]] = []

    monkeypatch.setattr(
        aggregation_module,
        "iter_lat_lon_runs",
        lambda run_dir, *, source_names: (
            calls.append(("standard", run_dir, source_names)) or iter(standard)
        ),
    )
    monkeypatch.setattr(
        aggregation_module,
        "iter_unique_text_lat_lon_runs",
        lambda run_dir, *, source_names: (
            calls.append(("text", run_dir, source_names)) or iter(text_only)
        ),
    )
    monkeypatch.setattr(
        aggregation_module,
        "assign_h3_cell",
        lambda lat, lon, *, resolution: f"{resolution}:{lat}:{lon}",
    )

    regular = aggregation_module.compute_polygon_density_summary(
        tmp_path,
        h3_resolution=7,
        source_names={"source.osm.pbf"},
    )
    extracted = aggregation_module.compute_polygon_density_summary(
        tmp_path,
        h3_resolution=8,
        extracted_text_only=True,
    )

    assert regular.polygon_row_count == 1
    assert regular.cells == (("7:1.0:2.0", 1),)
    assert extracted.polygon_row_count == 2
    assert extracted.cells == (("8:3.0:4.0", 2),)
    assert regular.extracted_text_only is False
    assert extracted.extracted_text_only is True
    assert calls == [
        ("standard", tmp_path, {"source.osm.pbf"}),
        ("text", tmp_path, None),
    ]


def test_summary_wraps_geographic_errors_with_source_and_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aggregation_module,
        "iter_lat_lon_runs",
        lambda _run_dir, *, source_names: iter([(Path("a.parquet"), 3, 91.0, 2.0)]),
    )
    monkeypatch.setattr(
        aggregation_module,
        "assign_h3_cell",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            aggregation_module.GeographicMapError("invalid coordinate")
        ),
    )

    with pytest.raises(ValueError, match=r"a\.parquet row 3: invalid coordinate"):
        aggregation_module.compute_polygon_density_summary(tmp_path)
