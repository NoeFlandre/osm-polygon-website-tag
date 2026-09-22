"""Deterministic polygon geometry statistics computed from public shards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.reporting import geometry_stats
from osm_polygon_website_tag.reporting.geometry_stats import (
    AREA_BUCKET_LABELS,
    GEOMETRY_STATS_SCHEMA_VERSION,
    NumericSummary,
    SourceAreaStats,
    _accumulate_area,
    _accumulate_extent,
    _accumulate_shape,
    _Accumulator,
    _area_bucket,
    _bbox_metre_values,
    _coordinates,
    _geodesic_lengths,
    _geometry_shape,
    _midpoints,
    _parse_bbox,
    _widen_bbox,
    compute_geometry_stats,
    render_geometry_stats,
)

_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.0, 0.001], [0.001, 0.001], [0.001, 0.0], [0.0, 0.0]]],
}
_SQUARE_BBOX = [0.0, 0.0, 0.001, 0.001]
_MULTIPOLYGON_WITH_HOLE = {
    "type": "MultiPolygon",
    "coordinates": [
        [
            [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]],
            [[0.2, 0.2], [0.2, 0.4], [0.4, 0.4], [0.4, 0.2], [0.2, 0.2]],
        ],
        [[[2.0, 2.0], [2.0, 3.0], [3.0, 3.0], [3.0, 2.0], [2.0, 2.0]]],
    ],
}


def _public_row(
    *,
    polygon_id: str = "monaco-latest:way/1",
    geometry: dict[str, Any] | None = None,
    bbox: list[float] | None = None,
    area_m2: float = 50.0,
    osm_primary_tag: str = "building",
) -> dict[str, Any]:
    """Return one current-schema public polygon row."""
    return {
        "polygon_id": polygon_id,
        "region": "monaco",
        "source_pbf": "monaco-latest.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_version": 1,
        "osm_timestamp": pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py(),
        "name": None,
        "website": "https://example.org",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.org",
        "contact_website_hostname": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": osm_primary_tag,
        "geometry": json.dumps(_SQUARE if geometry is None else geometry),
        "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
        "centroid_kind": "lambert_azimuthal_equal_area",
        "lat": 0.0,
        "lon": 0.0,
        "bbox": json.dumps(_SQUARE_BBOX if bbox is None else bbox),
        "area_m2": area_m2,
        "area_bucket": "10-100m2",
        "schema_version": "v1.3",
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "absent",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _write_shard(run_dir: Path, stem: str, rows: list[dict[str, Any]]) -> Path:
    """Write one public shard with the current polygon schema."""
    path = run_dir / "polygons" / f"{stem}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), path)
    return path


def test_empty_selection_reports_zeroed_statistics(tmp_path: Path) -> None:
    (tmp_path / "polygons").mkdir()

    stats = compute_geometry_stats(tmp_path)

    assert stats.schema_version == GEOMETRY_STATS_SCHEMA_VERSION
    assert stats.population_scope == "published polygon rows"
    assert stats.row_count == 0
    assert stats.area.summary == NumericSummary()
    assert stats.area.zero_area_row_count == 0
    assert stats.shape.polygon_row_count == 0
    assert stats.extent.bbox is None
    assert stats.per_source == []
    assert stats.per_osm_primary_tag == []
    assert [entry["row_count"] for entry in stats.area.histogram] == [0] * len(AREA_BUCKET_LABELS)


def test_report_includes_global_text_population_for_card_map_agreement(tmp_path: Path) -> None:
    first = _public_row(polygon_id="a:way/42")
    first.update(
        osm_id=42,
        website_text="old copy",
        website_word_count=2,
        website_text_status="success",
    )
    second = _public_row(polygon_id="b:way/42")
    second.update(
        osm_id=42,
        website_text="new copy",
        website_word_count=2,
        website_text_status="success",
        osm_version=2,
        lat=48.0,
        lon=2.0,
    )
    contact = _public_row(polygon_id="a:way/43")
    contact.update(
        osm_id=43,
        website=None,
        has_website=False,
        website_text=None,
        website_word_count=None,
        website_text_status="absent",
        contact_website="https://contact.example.org",
        has_contact_website=True,
        contact_website_text="contact copy",
        contact_website_word_count=2,
        contact_website_text_status="success",
    )
    _write_shard(tmp_path, "a", [first, contact])
    _write_shard(tmp_path, "b", [second])

    stats = compute_geometry_stats(tmp_path)
    payload = json.loads(render_geometry_stats(stats))

    assert stats.text_population.unique_identity_count == 2
    assert stats.text_population.website_identity_count == 1
    assert stats.text_population.contact_website_identity_count == 1
    assert stats.text_population.website_total_words == 2
    assert stats.text_population.contact_website_total_words == 2
    assert payload["text_population"]["unique_identity_count"] == 2


def test_missing_polygon_directory_is_reported_as_a_missing_artifact(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="polygons"):
        compute_geometry_stats(tmp_path)


def test_shard_without_the_geometry_columns_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "polygons"
    directory.mkdir()
    pq.write_table(pa.table({"polygon_id": ["a"]}), directory / "monaco-latest.parquet")

    with pytest.raises(ValueError, match="lacks geometry columns"):
        compute_geometry_stats(tmp_path)


def test_single_polygon_reports_exact_area_shape_and_extent(tmp_path: Path) -> None:
    _write_shard(tmp_path, "monaco-latest", [_public_row(area_m2=1234.5)])

    stats = compute_geometry_stats(tmp_path)

    assert stats.row_count == 1
    assert stats.area.summary.total == 1234.5
    assert stats.area.summary.minimum == 1234.5
    assert stats.area.summary.maximum == 1234.5
    assert stats.area.summary.mean == 1234.5
    assert stats.area.summary.median == 1234.5
    assert stats.area.summary.percentiles == dict.fromkeys(
        ("p1", "p5", "p25", "p50", "p75", "p95", "p99"), 1234.5
    )
    assert stats.area.zero_area_row_count == 0
    assert stats.area.below_one_m2_row_count == 0
    assert stats.shape.polygon_row_count == 1
    assert stats.shape.multipolygon_row_count == 0
    assert stats.shape.with_holes_row_count == 0
    assert stats.shape.rings_per_row.maximum == 1.0
    assert stats.shape.vertices_per_row.maximum == 5.0
    assert stats.shape.components_per_multipolygon == NumericSummary()
    assert stats.extent.bbox == _SQUARE_BBOX
    assert stats.extent.width_degrees.maximum == 0.001
    assert stats.extent.width_m.maximum == pytest.approx(111.3, abs=0.5)
    assert stats.per_source == [
        SourceAreaStats(
            source_pbf="monaco-latest.osm.pbf",
            row_count=1,
            area_m2=stats.area.summary,
        )
    ]
    assert [(entry.osm_primary_tag, entry.row_count) for entry in stats.per_osm_primary_tag] == [
        ("building", 1)
    ]


def test_multipolygon_with_holes_counts_components_rings_and_holes(tmp_path: Path) -> None:
    _write_shard(
        tmp_path,
        "monaco-latest",
        [
            _public_row(
                geometry=_MULTIPOLYGON_WITH_HOLE,
                bbox=[0.0, 0.0, 3.0, 3.0],
                area_m2=2.0,
            ),
            _public_row(polygon_id="monaco-latest:way/2", area_m2=0.0),
        ],
    )

    stats = compute_geometry_stats(tmp_path)

    assert stats.shape.multipolygon_row_count == 1
    assert stats.shape.polygon_row_count == 1
    assert stats.shape.with_holes_row_count == 1
    assert stats.shape.hole_ring_count == 1
    assert stats.shape.components_per_multipolygon.row_count == 1
    assert stats.shape.components_per_multipolygon.maximum == 2.0
    assert stats.shape.rings_per_row.maximum == 3.0
    assert stats.shape.vertices_per_row.maximum == 15.0
    assert stats.area.zero_area_row_count == 1
    assert stats.area.below_one_m2_row_count == 1
    assert stats.extent.bbox == [0.0, 0.0, 3.0, 3.0]


def test_multi_source_run_breaks_every_shard_down_in_sorted_order(tmp_path: Path) -> None:
    _write_shard(tmp_path, "b-latest", [_public_row(area_m2=100.0, osm_primary_tag="amenity")])
    _write_shard(
        tmp_path,
        "a-latest",
        [_public_row(area_m2=10.0), _public_row(polygon_id="a:way/2", area_m2=30.0)],
    )

    stats = compute_geometry_stats(tmp_path)

    assert [(entry.source_pbf, entry.row_count) for entry in stats.per_source] == [
        ("a-latest.osm.pbf", 2),
        ("b-latest.osm.pbf", 1),
    ]
    assert stats.per_source[0].area_m2.total == 40.0
    assert stats.per_source[1].area_m2.total == 100.0
    assert stats.row_count == 3
    assert stats.area.summary.total == 140.0
    assert [(entry.osm_primary_tag, entry.row_count) for entry in stats.per_osm_primary_tag] == [
        ("building", 2),
        ("amenity", 1),
    ]
    assert stats.per_osm_primary_tag[1].total_area_m2 == 100.0


def test_selected_sources_limit_the_computation(tmp_path: Path) -> None:
    _write_shard(tmp_path, "a-latest", [_public_row(area_m2=10.0)])
    _write_shard(tmp_path, "b-latest", [_public_row(area_m2=100.0)])

    stats = compute_geometry_stats(tmp_path, source_names=["b-latest.osm.pbf"])

    assert [entry.source_pbf for entry in stats.per_source] == ["b-latest.osm.pbf"]
    assert stats.area.summary.total == 100.0


def test_repeated_computation_renders_byte_identical_json(tmp_path: Path) -> None:
    _write_shard(
        tmp_path,
        "a-latest",
        [_public_row(area_m2=area) for area in (3.0, 1.0, 2.0)],
    )
    _write_shard(tmp_path, "b-latest", [_public_row(area_m2=1e11)])

    first = render_geometry_stats(compute_geometry_stats(tmp_path))
    second = render_geometry_stats(compute_geometry_stats(tmp_path))

    assert first == second
    assert json.loads(first)["row_count"] == 4
    assert first.endswith("}\n")


def test_antimeridian_and_polar_bounding_boxes_are_counted(tmp_path: Path) -> None:
    _write_shard(
        tmp_path,
        "a-latest",
        [
            _public_row(bbox=[-179.0, 0.0, 179.0, 1.0]),
            _public_row(polygon_id="a:way/2", bbox=[0.0, 85.5, 1.0, 86.0]),
            _public_row(polygon_id="a:way/3", bbox=[0.0, -86.0, 1.0, -85.5]),
        ],
    )

    stats = compute_geometry_stats(tmp_path)

    assert stats.extent.antimeridian_row_count == 1
    assert stats.extent.polar_row_count == 2
    assert stats.extent.bbox == [-179.0, -86.0, 179.0, 86.0]


def test_area_buckets_are_stable_log_decades() -> None:
    assert AREA_BUCKET_LABELS == (
        "0",
        "<1e0",
        "1e0-1e1",
        "1e1-1e2",
        "1e2-1e3",
        "1e3-1e4",
        "1e4-1e5",
        "1e5-1e6",
        "1e6-1e7",
        "1e7-1e8",
        "1e8-1e9",
        "1e9-1e10",
        ">=1e10",
    )
    assert _area_bucket(0.0) == "0"
    assert _area_bucket(0.5) == "<1e0"
    assert _area_bucket(1.0) == "1e0-1e1"
    assert _area_bucket(9.999) == "1e0-1e1"
    assert _area_bucket(10.0) == "1e1-1e2"
    assert _area_bucket(9.9e9) == "1e9-1e10"
    assert _area_bucket(1e10) == ">=1e10"


def test_histogram_counts_every_bucket_of_a_run(tmp_path: Path) -> None:
    _write_shard(
        tmp_path,
        "a-latest",
        [_public_row(area_m2=area) for area in (0.0, 0.5, 5.0, 5.5, 1e10)],
    )

    stats = compute_geometry_stats(tmp_path)

    counts = {entry["bucket"]: entry["row_count"] for entry in stats.area.histogram}
    assert counts["0"] == 1
    assert counts["<1e0"] == 1
    assert counts["1e0-1e1"] == 2
    assert counts[">=1e10"] == 1
    assert sum(counts.values()) == 5
    assert [entry["bucket"] for entry in stats.area.histogram] == list(AREA_BUCKET_LABELS)


def test_percentiles_use_exact_nearest_rank_over_every_value_in_a_shard(tmp_path: Path) -> None:
    _write_shard(
        tmp_path,
        "a-latest",
        [
            _public_row(polygon_id=f"a:way/{value}", area_m2=float(value))
            for value in range(1, 1001)
        ],
    )

    summary = compute_geometry_stats(tmp_path).area.summary

    assert summary.percentiles["p1"] == 10.0
    assert summary.percentiles["p50"] == 500.0
    assert summary.percentiles["p99"] == 990.0


def test_area_accumulation_records_buckets_degenerates_and_tags() -> None:
    accumulator = _Accumulator()

    _accumulate_area(accumulator, 0.0, "building")
    _accumulate_area(accumulator, 0.5, "building")
    _accumulate_area(accumulator, 25.0, "amenity")

    assert accumulator.row_count == 3
    assert accumulator.zero_area_row_count == 1
    assert accumulator.below_one_m2_row_count == 2
    assert dict(accumulator.buckets) == {"0": 1, "<1e0": 1, "1e1-1e2": 1}


def test_shape_accumulation_separates_polygons_multipolygons_and_holes() -> None:
    accumulator = _Accumulator()

    assert _accumulate_shape(accumulator, json.dumps(_SQUARE)) == ("Polygon", 1, 1, 5)
    assert _accumulate_shape(accumulator, json.dumps(_MULTIPOLYGON_WITH_HOLE)) == (
        "MultiPolygon",
        2,
        3,
        15,
    )
    assert _accumulate_shape(accumulator, json.dumps({"type": "Polygon", "coordinates": []})) == (
        "Polygon",
        1,
        0,
        0,
    )

    assert accumulator.polygon_row_count == 2
    assert accumulator.multipolygon_row_count == 1
    assert accumulator.with_holes_row_count == 1
    assert accumulator.hole_ring_count == 1
    assert accumulator.row_count == 0


def test_extent_accumulation_widens_the_box_and_flags_edge_rows() -> None:
    accumulator = _Accumulator()

    assert _accumulate_extent(accumulator, (1.0, 2.0, 3.0, 5.0)) == (2.0, 3.0)
    assert _accumulate_extent(accumulator, (-179.0, -86.0, 179.0, -85.5)) == (358.0, 0.5)
    assert accumulator.antimeridian_row_count == 1
    assert accumulator.polar_row_count == 1
    assert accumulator.bbox == [-179.0, -86.0, 179.0, 5.0]


def test_bbox_metre_accumulation_measures_the_mid_axis_geodesics() -> None:
    assert _bbox_metre_values([]) == ([], [])

    widths_m, heights_m = _bbox_metre_values([(0.0, 0.0, 1.0, 2.0)])

    mid_lat, mid_lon = 1.0, 0.5
    assert widths_m == _geodesic_lengths([0.0], [mid_lat], [1.0], [mid_lat])
    assert heights_m == _geodesic_lengths([mid_lon], [0.0], [mid_lon], [2.0])


def test_widen_bbox_returns_the_outer_envelope() -> None:
    assert _widen_bbox(None, (1.0, 2.0, 3.0, 4.0)) == [1.0, 2.0, 3.0, 4.0]
    assert _widen_bbox([1.0, 2.0, 3.0, 4.0], (0.0, 5.0, 2.0, 9.0)) == [0.0, 2.0, 3.0, 9.0]


def test_batch_axis_helpers_project_one_coordinate_each() -> None:
    boxes = [(0.0, 1.0, 2.0, 5.0), (10.0, 20.0, 30.0, 40.0)]

    assert _coordinates(boxes, 0) == [0.0, 10.0]
    assert _coordinates(boxes, 3) == [5.0, 40.0]
    assert _midpoints(boxes, 0, 2) == [1.0, 20.0]
    assert _midpoints(boxes, 1, 3) == [3.0, 30.0]


def test_geodesic_lengths_measure_each_segment_in_metres() -> None:
    lengths = _geodesic_lengths([0.0, 0.0], [0.0, 0.0], [0.0, 1.0], [1.0, 0.0])

    assert len(lengths) == 2
    assert lengths[0] == pytest.approx(110_574.4, abs=1.0)
    assert lengths[1] == pytest.approx(111_319.5, abs=1.0)


def test_summary_of_an_empty_distribution_is_zeroed(tmp_path: Path) -> None:
    (tmp_path / "polygons").mkdir()

    assert compute_geometry_stats(tmp_path).area.summary == NumericSummary()


def test_summary_rounds_and_totals_exactly(tmp_path: Path) -> None:
    _write_shard(
        tmp_path,
        "a-latest",
        [
            _public_row(polygon_id=f"a:way/{i}", area_m2=area)
            for i, area in enumerate((1.0, 2.0, 1e16))
        ],
    )

    summary = compute_geometry_stats(tmp_path).area.summary

    assert summary.total == 1e16 + 3.0
    assert summary.mean == round((1e16 + 3.0) / 3, 6)
    assert summary.median == 2.0
    assert summary.row_count == 3


def test_geometry_shape_rejects_unsupported_types_and_counts_empty_coordinates() -> None:
    assert _geometry_shape(json.dumps({"type": "Polygon", "coordinates": []})) == (
        "Polygon",
        1,
        0,
        0,
    )
    assert _geometry_shape(json.dumps(_MULTIPOLYGON_WITH_HOLE)) == ("MultiPolygon", 2, 3, 15)
    with pytest.raises(ValueError, match="unsupported geometry type"):
        _geometry_shape(json.dumps({"type": "LineString", "coordinates": []}))


def test_bbox_parsing_rejects_malformed_values() -> None:
    assert _parse_bbox("[1, 2, 3, 4]") == (1.0, 2.0, 3.0, 4.0)
    with pytest.raises(ValueError, match="invalid bbox value"):
        _parse_bbox("[1, 2, 3]")


def test_rows_without_geometry_still_report_an_empty_polygon_shape(tmp_path: Path) -> None:
    _write_shard(
        tmp_path,
        "a-latest",
        [_public_row(geometry={"type": "Polygon", "coordinates": []}, area_m2=0.0)],
    )

    stats = compute_geometry_stats(tmp_path)

    assert stats.shape.with_holes_row_count == 0
    assert stats.shape.rings_per_row.maximum == 0.0
    assert stats.shape.vertices_per_row.maximum == 0.0


def test_geometry_values_are_computed_serially(tmp_path: Path) -> None:
    """Float aggregates must stay serial: parallel summation is not associative."""
    store = geometry_stats._GeometryValueStore(tmp_path)
    try:
        row = store._connection.execute("SELECT current_setting('threads')").fetchone()
        assert row is not None
        assert int(row[0]) == 1
    finally:
        store.close()


def test_render_geometry_stats_is_sorted_indented_json() -> None:
    from dataclasses import asdict

    stats = geometry_stats.GeometryStats(row_count=3)
    payload = asdict(stats)
    assert list(payload) != sorted(payload), "fixture must not already be key-sorted"

    rendered = render_geometry_stats(stats)

    assert rendered == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    assert rendered.startswith('{\n  "')


def test_accumulate_batch_spills_every_row_under_its_schema_names() -> None:
    batch = pa.RecordBatch.from_pydict(
        {
            "bbox": ["[0.0, 0.0, 1.0, 2.0]"],
            "geometry": [
                json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 2], [0, 0]]]})
            ],
            "area_m2": [5.0],
            "osm_primary_tag": ["building"],
        }
    )
    spilled: list[list[dict[str, Any]]] = []
    store = type("Store", (), {"append": lambda _self, rows: spilled.append(rows)})()
    accumulator = geometry_stats._Accumulator()

    geometry_stats._accumulate_batch(
        accumulator,
        batch,
        source_pbf="a.osm.pbf",
        store=store,  # type: ignore
    )

    [rows] = spilled
    [row] = rows
    assert sorted(row) == sorted(geometry_stats._SPILL_SCHEMA.names)
    assert row["width_degrees"] == 1.0
    assert row["height_degrees"] == 2.0
    assert row["height_m"] > row["width_m"] > 0
    assert row["source_pbf"] == "a.osm.pbf"
    assert row["osm_primary_tag"] == "building"
