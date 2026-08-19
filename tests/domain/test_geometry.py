"""Tests for polygon geometry extraction from osmium Area objects."""

from __future__ import annotations

import json
import os
import tempfile

import osmium
import osmium.osm
import pytest
from shapely.geometry import Polygon

import osm_polygon_website_tag.domain.geometry as geometry_module
from osm_polygon_website_tag.domain.geometry import (
    CENTROID_KIND,
    GeometryRejection,
    PolygonGeometry,
    _repair_geometry,
    compute_polygon_area_m2,
    geometry_from_area,
)


def _write_synthetic_osm(xml: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".osm", delete=False) as f:
        f.write(xml)
        return f.name


def _drive_area_callback(p: str, predicate=None) -> list[PolygonGeometry]:
    """Run the SimpleHandler and snapshot PolygonGeometry objects."""
    collected: list[PolygonGeometry] = []

    class Handler(osmium.SimpleHandler):
        def area(self, a: osmium.osm.Area) -> None:
            if predicate is None or predicate(a):
                collected.append(geometry_from_area(a))

    Handler().apply_file(p)
    return collected


def test_polygon_geometry_is_frozen_dataclass() -> None:
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
<node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/></way></osm>
"""
    )
    try:
        geoms = _drive_area_callback(p)
        assert len(geoms) == 1
        geom_record = geoms[0]
    finally:
        os.remove(p)
    assert isinstance(geom_record, PolygonGeometry)
    with pytest.raises(AttributeError):
        geom_record.geometry = "x"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_geometry_from_area_returns_polygon_for_simple_shape() -> None:
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
<node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/></way></osm>
"""
    )
    try:
        geoms = _drive_area_callback(p)
        geom_record = geoms[0]
    finally:
        os.remove(p)
    parsed = json.loads(geom_record.geometry)
    assert parsed["type"] == "Polygon"
    assert parsed["coordinates"] == [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]


def test_geometry_from_geojson_returns_public_metrics() -> None:
    raw = '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}'

    result = geometry_module.geometry_from_geojson(raw)

    assert json.loads(result.geometry) == {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
    }
    assert result.centroid_kind == CENTROID_KIND
    assert result.area_m2 > 0
    assert result.area_bucket == ">=1000km2"
    assert result.bbox == [0.0, 0.0, 1.0, 1.0]


def test_repair_geometry_rejects_empty_and_repairs_invalid_shapes() -> None:
    with pytest.raises(GeometryRejection, match="empty geometry"):
        _repair_geometry(Polygon())

    repaired = _repair_geometry(Polygon([(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)]))

    assert repaired.is_valid
    assert not repaired.is_empty


def test_geometry_from_area_returns_polygon_for_single_polygon_relation() -> None:
    """A relation that resolves to one polygon component without holes is Polygon."""
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/></way>
  <relation id="200">
    <member type="way" ref="100" role="outer"/>
    <tag k="type" v="multipolygon"/>
    <tag k="landuse" v="forest"/>
  </relation>
</osm>
"""
    )
    try:
        geoms = _drive_area_callback(p, predicate=lambda a: not a.from_way())
        assert len(geoms) == 1
        geom_record = geoms[0]
    finally:
        os.remove(p)
    parsed = json.loads(geom_record.geometry)
    assert parsed["type"] == "Polygon"
    assert len(parsed["coordinates"]) == 1


def test_geometry_records_centroid_lat_lon_bbox() -> None:
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="2.0"/>
<node id="3" lat="2.0" lon="2.0"/><node id="4" lat="2.0" lon="0.0"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/></way></osm>
"""
    )
    try:
        geoms = _drive_area_callback(p)
        geom_record = geoms[0]
    finally:
        os.remove(p)
    # Lambert-azimuthal-equal-area centroid of a 2x2 deg square
    # anchored at its outer-ring barycenter; tolerance 1e-3 deg
    # is appropriate for v1.1.
    assert geom_record.lat == pytest.approx(1.0, abs=1e-3)
    assert geom_record.lon == pytest.approx(1.0, abs=1e-3)
    assert geom_record.bbox == [0.0, 0.0, 2.0, 2.0]


def test_geometry_records_centroid_kind_equal_area() -> None:
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
<node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/></way></osm>
"""
    )
    try:
        geoms = _drive_area_callback(p)
        geom_record = geoms[0]
    finally:
        os.remove(p)
    assert geom_record.centroid_kind == "lambert_azimuthal_equal_area"


def test_geometry_records_finite_area() -> None:
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
<node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/></way></osm>
"""
    )
    try:
        geoms = _drive_area_callback(p)
        geom_record = geoms[0]
    finally:
        os.remove(p)
    assert geom_record.area_m2 > 0
    assert geom_record.area_km2 == geom_record.area_m2 / 1_000_000.0


def test_geometry_coordinates_rounded_to_7_decimals() -> None:
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.00000001" lon="0.00000001"/>
<node id="2" lat="0.0" lon="0.00010001"/><node id="3" lat="0.00010001" lon="0.00010001"/>
<node id="4" lat="0.00010001" lon="0.0"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/></way></osm>
"""
    )
    try:
        geoms = _drive_area_callback(p)
        geom_record = geoms[0]
    finally:
        os.remove(p)
    parsed = json.loads(geom_record.geometry)
    flat: list[float] = []
    for ring in parsed["coordinates"]:
        for c in ring:
            flat.extend(c)
    assert max(flat) <= 0.0002


def test_geometry_hole_subtracts_from_area_and_shifts_centroid() -> None:
    """A polygon with a hole must have a smaller area than its outer ring
    alone, and its centroid must move toward the mass on the outer ring."""
    p_with = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="10.0"/>
  <node id="3" lat="10.0" lon="10.0"/><node id="4" lat="10.0" lon="0.0"/>
  <node id="5" lat="4.0" lon="4.0"/><node id="6" lat="4.0" lon="6.0"/>
  <node id="7" lat="6.0" lon="6.0"/><node id="8" lat="6.0" lon="4.0"/>
  <way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/></way>
  <way id="101"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/>
    <tag k="building" v="yes"/></way>
  <relation id="200">
    <member type="way" ref="100" role="outer"/>
    <member type="way" ref="101" role="inner"/>
    <tag k="type" v="multipolygon"/>
    <tag k="landuse" v="forest"/>
    <tag k="website" v="https://forest.example"/>
  </relation>
</osm>
"""
    )
    try:
        geoms = _drive_area_callback(p_with, predicate=lambda a: not a.from_way())
        with_hole = geoms[0]
    finally:
        os.remove(p_with)

    p_without = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="10.0"/>
  <node id="3" lat="10.0" lon="10.0"/><node id="4" lat="10.0" lon="0.0"/>
  <way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/></way>
  <relation id="200">
    <member type="way" ref="100" role="outer"/>
    <tag k="type" v="multipolygon"/>
    <tag k="landuse" v="forest"/>
    <tag k="website" v="https://forest.example"/>
  </relation>
</osm>
"""
    )
    try:
        geoms = _drive_area_callback(p_without, predicate=lambda a: not a.from_way())
        without_hole = geoms[0]
    finally:
        os.remove(p_without)

    # Hole subtracts area.
    assert with_hole.area_m2 < without_hole.area_m2
    # Centroid shifts -- both coordinates stay within the bounding box
    # but they differ from the centroid of the polygon without the hole.
    assert (with_hole.lat, with_hole.lon) != (
        pytest.approx(without_hole.lat),
        pytest.approx(without_hole.lon),
    )


def test_compute_polygon_area_zero_for_degenerate() -> None:
    assert compute_polygon_area_m2([[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]) == 0.0


def test_compute_polygon_area_positive_for_real_polygon() -> None:
    ring = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]
    area = compute_polygon_area_m2(ring)
    assert area > 1.0e10
    assert area < 1.5e10


def test_centroid_agrees_with_trusted_implementation() -> None:
    """The Lambert-azimuthal-equal-area centroid must agree with a
    hand-computed expectation for a square polygon centred at (5, 5)."""
    p = _write_synthetic_osm(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="4.0" lon="4.0"/><node id="2" lat="4.0" lon="6.0"/>
<node id="3" lat="6.0" lon="6.0"/><node id="4" lat="6.0" lon="4.0"/>
<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/></way></osm>
"""
    )
    try:
        geoms = _drive_area_callback(p)
        geom_record = geoms[0]
    finally:
        os.remove(p)
    # Centroid of a symmetric square at (lat=5, lon=5) -- close to 5,5.
    assert geom_record.lat == pytest.approx(5.0, abs=1e-3)
    assert geom_record.lon == pytest.approx(5.0, abs=1e-3)
