"""Polygon geometry extraction from osmium Area objects.

This module wraps libosmium's ``osmium.area.AreaManager`` output and
produces the deterministic GeoJSON strings, centroids, bboxes, and
geodesic areas that the public dataset requires.

Public surface
--------------

* :class:`PolygonGeometry` -- frozen record carrying the public-facing
  geometry, centroid, bbox, and area.
* :func:`geometry_from_area` -- build a :class:`PolygonGeometry` from an
  osmium ``Area``.
* :func:`geometry_from_geojson` -- build the same result from serialized
  GeoJSON, allowing extraction workers to avoid sharing live osmium objects.
* :func:`compute_polygon_area_m2` -- geodesic area on WGS84 for a
  closed ``[lon, lat]`` ring using ``pyproj.Geod``.
* :class:`GeometryRejection` -- raised when a geometry cannot be
  assembled; ``kind`` attribute is a short identifier.

Geometry pipeline
-----------------

1. ``osmium.geom.GeoJSONFactory().create_multipolygon(area)`` produces a
   GeoJSON string.
2. The string is parsed via :mod:`json` and rebuilt with :mod:`shapely`
   so exterior/interior association per polygon component is preserved.
3. Coordinate rounding to seven decimals is applied once, after ring
   extraction, before any metric computation.
4. Antimeridian rings (lon-span > 180 degrees) are rejected with
   :class:`GeometryRejection` carrying ``kind="antimeridian"``.
5. The geometry is classified as ``Polygon`` or ``MultiPolygon`` from
   its Shapely structure, not from the OSM source type: a relation
   that resolves to one polygon component without holes is ``Polygon``.
6. Geodesic area (outer minus inner ring) is computed with
   ``pyproj.Geod.geometry_area_perimeter``.
7. The centroid is computed in a Lambert azimuthal equal-area
   projection centred on the polygon's area-weighted outer-ring
   barycenter, then reprojected back to WGS84. The ``centroid_kind``
   field on :class:`PolygonGeometry` always reports
   ``"lambert_azimuthal_equal_area"``. This is NOT a geodesic centroid.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Final

import osmium
import osmium.geom
import osmium.osm
import pyproj
from shapely.geometry import LinearRing, MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import transform as shapely_transform

EARTH_RADIUS_M: Final[float] = 6_378_137.0
COORDINATE_PRECISION: Final[int] = 7
CENTROID_KIND: Final[str] = "lambert_azimuthal_equal_area"


class GeometryRejection(ValueError):  # noqa: N818
    """Raised when a geometry cannot be assembled.

    The ``kind`` attribute is a short identifier consumed by the
    extractor when deciding how to record the rejection.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class PolygonGeometry:
    """A polygon's public-facing geometry, centroid, bbox, and area."""

    geometry: str
    centroid: str
    centroid_kind: str
    lat: float
    lon: float
    bbox: list[float]
    area_m2: float
    area_km2: float
    area_bucket: str


def _round_coord(value: float) -> float:
    return round(float(value), COORDINATE_PRECISION)


def _round_ring(ring_coords: list[list[float]]) -> list[list[float]]:
    return [[_round_coord(x), _round_coord(y)] for (x, y) in ring_coords]


def _area_bucket(area_m2: float) -> str:
    limits = (10.0, 100.0, 1_000_000.0, 10_000_000.0, 100_000_000.0, 1_000_000_000.0)
    labels = (
        "<10m2",
        "10-100m2",
        "100m2-1km2",
        "1-10km2",
        "10-100km2",
        "100km2-1000km2",
        ">=1000km2",
    )
    return labels[bisect_right(limits, area_m2)]


def _check_antimeridian(rings: list[list[list[float]]]) -> None:
    for ring in rings:
        if len(ring) < 2:
            continue
        lons = [pt[0] for pt in ring]
        if max(lons) - min(lons) > 180.0:
            raise GeometryRejection(
                kind="antimeridian",
                message=("ring crosses antimeridian (lon-span > 180 degrees); rejected in v1.1"),
            )


def _shapely_to_rounded_multipoly_coords(
    shapely_geom: Polygon | MultiPolygon,
) -> list[list[list[list[float]]]]:
    """Walk the Shapely geometry and emit rounded multipolygon coords."""
    polys = shapely_geom.geoms if isinstance(shapely_geom, MultiPolygon) else [shapely_geom]
    return [_rounded_polygon_rings(p) for p in polys]


def _rounded_polygon_rings(polygon: Polygon) -> list[list[list[float]]]:
    """Return one polygon's rounded exterior and interior rings."""
    rings = [_round_ring([[x, y] for (x, y) in polygon.exterior.coords])]
    rings.extend(
        _round_ring([[x, y] for (x, y) in interior.coords]) for interior in polygon.interiors
    )
    return rings


def _largest_polygon(mp: MultiPolygon) -> Polygon:
    return max(mp.geoms, key=lambda p: p.area)


def _compute_geodesic_area_m2(shapely_geom: Polygon | MultiPolygon) -> float:
    """Outer-ring geodesic area minus the absolute inner-ring area per component."""
    if shapely_geom.is_empty:
        raise GeometryRejection(kind="empty_geometry", message="empty geometry")
    geod = pyproj.Geod(ellps="WGS84")
    polys = shapely_geom.geoms if isinstance(shapely_geom, MultiPolygon) else [shapely_geom]
    return float(sum(_polygon_geodesic_area(geod, polygon) for polygon in polys))


def _polygon_geodesic_area(geod: pyproj.Geod, polygon: Polygon) -> float:
    """Return one polygon's validated net geodesic area."""
    outer_area = _checked_ring_area(geod, polygon.exterior, "outer-ring")
    inner_total = sum(
        abs(_checked_ring_area(geod, interior, "inner-ring")) for interior in polygon.interiors
    )
    net = abs(outer_area) - inner_total
    if not math.isfinite(net):
        raise GeometryRejection(
            kind="non_finite_area",
            message=f"net geodesic area is not finite: {net!r}",
        )
    if net < 0.0:
        raise GeometryRejection(
            kind="degenerate_geometry",
            message=f"net geodesic area is negative: {net!r}",
        )
    return net


def _checked_ring_area(geod: pyproj.Geod, ring: LinearRing, label: str) -> float:
    """Compute one ring area and reject non-finite results."""
    area, _ = geod.geometry_area_perimeter(ring)
    if not math.isfinite(area):
        raise GeometryRejection(
            kind="non_finite_area",
            message=f"{label} geodesic area is not finite: {area!r}",
        )
    return area


def _compute_centroid_lonlat(shapely_geom: Polygon | MultiPolygon) -> tuple[float, float]:
    """Lambert-azimuthal-equal-area centroid of ``shapely_geom`` in WGS84."""
    anchor_poly = _centroid_anchor(shapely_geom)
    lon0, lat0 = _outer_ring_barycenter(anchor_poly)
    return _projected_centroid(shapely_geom, lon0, lat0)


def _centroid_anchor(shapely_geom: Polygon | MultiPolygon) -> Polygon:
    """Choose and validate the polygon used to anchor the centroid projection."""
    anchor_poly = (
        _largest_polygon(shapely_geom) if isinstance(shapely_geom, MultiPolygon) else shapely_geom
    )
    if anchor_poly.is_empty:
        raise GeometryRejection(kind="degenerate_geometry", message="anchor polygon is empty")
    if len(anchor_poly.exterior.coords) < 4:
        raise GeometryRejection(
            kind="degenerate_geometry", message="anchor outer ring has fewer than 4 coordinates"
        )
    return anchor_poly


def _outer_ring_barycenter(anchor_poly: Polygon) -> tuple[float, float]:
    """Return the perimeter-weighted outer-ring barycenter."""
    geod = pyproj.Geod(ellps="WGS84")
    _validate_outer_ring_area(geod, anchor_poly)
    pts = list(zip(*anchor_poly.exterior.coords.xy, strict=True))
    sum_lon, sum_lat, sum_w = _weighted_ring_sums(geod, pts)
    if sum_w == 0.0 or not math.isfinite(sum_w):
        raise GeometryRejection(
            kind="non_finite_area",
            message="outer-ring barycenter weight sum is zero or non-finite",
        )
    return sum_lon / sum_w, sum_lat / sum_w


def _validate_outer_ring_area(geod: pyproj.Geod, polygon: Polygon) -> None:
    """Reject an empty, zero-area, or non-finite outer ring."""
    outer_area_signed, _ = geod.geometry_area_perimeter(polygon.exterior)
    if outer_area_signed == 0.0 or not math.isfinite(outer_area_signed):
        raise GeometryRejection(
            kind="non_finite_area",
            message=f"outer-ring geodesic area is not finite: {outer_area_signed!r}",
        )


def _weighted_ring_sums(
    geod: pyproj.Geod,
    points: list[tuple[float, float]],
) -> tuple[float, float, float]:
    """Return longitude, latitude, and perimeter-weight sums."""

    # Compute area-weighted outer-ring barycenter in WGS84.
    sum_lon = 0.0
    sum_lat = 0.0
    sum_w = 0.0
    for i in range(len(points) - 1):
        lon1, lat1 = points[i]
        lon2, lat2 = points[i + 1]
        _, _, dx = geod.inv(lon1, lat1, lon2, lat2)
        weight = abs(dx)
        if weight == 0.0 or not math.isfinite(weight):
            continue
        sum_lon += (lon1 + lon2) / 2.0 * weight
        sum_lat += (lat1 + lat2) / 2.0 * weight
        sum_w += weight
    return sum_lon, sum_lat, sum_w


def _projected_centroid(
    shapely_geom: Polygon | MultiPolygon,
    lon0: float,
    lat0: float,
) -> tuple[float, float]:
    """Project a geometry to its local equal-area CRS and return its centroid."""
    proj_string = f"+proj=laea +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +ellps=WGS84"
    crs = pyproj.CRS.from_proj4(proj_string)

    def _to_xy(x: float, y: float, z: Any = None) -> tuple[float, float]:
        return pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(x, y)

    def _to_ll(x: float, y: float, z: Any = None) -> tuple[float, float]:
        return pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(x, y)

    projected = shapely_transform(_to_xy, shapely_geom)
    if projected.is_empty:
        raise GeometryRejection(kind="degenerate_geometry", message="projected geometry is empty")
    centroid_proj = projected.centroid
    if centroid_proj.is_empty:
        raise GeometryRejection(kind="degenerate_geometry", message="projected centroid is empty")
    centroid_ll = shapely_transform(_to_ll, centroid_proj)
    return float(centroid_ll.x), float(centroid_ll.y)


def geometry_from_geojson(raw_geojson: str) -> PolygonGeometry:
    """Build a :class:`PolygonGeometry` from serialized GeoJSON."""
    shapely_geom = _parse_geojson_geometry(json.loads(raw_geojson))
    shapely_geom = _repair_geometry(shapely_geom)
    geometry_str = _serialise_geometry(shapely_geom)
    minx, miny, maxx, maxy = shapely_geom.bounds
    bbox = [_round_coord(minx), _round_coord(miny), _round_coord(maxx), _round_coord(maxy)]
    area_m2 = _compute_geodesic_area_m2(shapely_geom)
    area_km2 = area_m2 / 1_000_000.0
    centroid_lon, centroid_lat = _compute_centroid_lonlat(shapely_geom)
    centroid_lat_r = _round_coord(centroid_lat)
    centroid_lon_r = _round_coord(centroid_lon)
    centroid_geojson = json.dumps(
        {"type": "Point", "coordinates": [centroid_lon_r, centroid_lat_r]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return PolygonGeometry(
        geometry=geometry_str,
        centroid=centroid_geojson,
        centroid_kind=CENTROID_KIND,
        lat=centroid_lat_r,
        lon=centroid_lon_r,
        bbox=bbox,
        area_m2=area_m2,
        area_km2=area_km2,
        area_bucket=_area_bucket(area_m2),
    )


def _parse_geojson_geometry(parsed: Any) -> Polygon | MultiPolygon:
    """Parse and orient a Polygon or MultiPolygon GeoJSON object."""
    if parsed.get("type") == "Polygon":
        rings = parsed["coordinates"]
        _check_antimeridian(rings)
        return orient(Polygon(rings[0], rings[1:]), sign=1.0)
    if parsed.get("type") == "MultiPolygon":
        coordinates = parsed["coordinates"]
        for poly in coordinates:
            _check_antimeridian(poly)
        return orient(MultiPolygon([Polygon(p[0], p[1:]) for p in coordinates]), sign=1.0)
    raise GeometryRejection(
        kind="unknown_geometry_type",
        message=f"unsupported geometry type: {parsed.get('type')!r}",
    )


def _repair_geometry(shapely_geom: Polygon | MultiPolygon) -> Polygon | MultiPolygon:
    """Reject empty geometries and repair invalid ones with Shapely."""
    if shapely_geom.is_empty:
        raise GeometryRejection(kind="empty_geometry", message="empty geometry")
    if not shapely_geom.is_valid:
        repaired = shapely_geom.buffer(0)
        if repaired.is_empty:
            raise GeometryRejection(
                kind="degenerate_geometry",
                message="geometry could not be repaired",
            )
        shapely_geom = repaired
    return shapely_geom


def _serialise_geometry(shapely_geom: Polygon | MultiPolygon) -> str:
    """Round and serialise a Shapely polygon geometry deterministically."""
    rebuilt = _shapely_to_rounded_multipoly_coords(shapely_geom)
    if len(rebuilt) == 1:
        geometry_obj: dict[str, Any] = {"type": "Polygon", "coordinates": rebuilt[0]}
    else:
        geometry_obj = {"type": "MultiPolygon", "coordinates": rebuilt}
    return json.dumps(geometry_obj, sort_keys=True, separators=(",", ":"))


def geometry_from_area(area: osmium.osm.Area) -> PolygonGeometry:
    """Build a :class:`PolygonGeometry` from an osmium ``Area``."""
    factory = osmium.geom.GeoJSONFactory()
    raw_geojson: Any = factory.create_multipolygon(area)
    return geometry_from_geojson(raw_geojson)


def compute_polygon_area_m2(ring: list[list[float]]) -> float:
    """Return the geodesic area (m^2) of a single closed ``[lon, lat]`` ring.

    Uses ``pyproj.Geod.geometry_area_perimeter`` on a Shapely
    ``LinearRing``. Rings with fewer than three distinct points return
    ``0.0``. Non-finite results are coerced to ``0.0``.
    """
    pts = _open_ring_points(ring)
    if len(pts) < 3:
        return 0.0
    shapely_ring = LinearRing([(p[0], p[1]) for p in pts])
    geod = pyproj.Geod(ellps="WGS84")
    try:
        return _finite_abs_area(geod, shapely_ring)
    except Exception:
        return 0.0


def _open_ring_points(ring: list[list[float]]) -> list[list[float]]:
    """Return a ring without its repeated closing point when present."""
    if len(ring) < 4:
        return []
    if (
        abs(ring[0][0] - ring[-1][0]) < 1e-12
        and abs(ring[0][1] - ring[-1][1]) < 1e-12
        and len(ring) > 1
    ):
        return ring[:-1]
    return ring


def _finite_abs_area(geod: pyproj.Geod, ring: LinearRing) -> float:
    """Return a finite absolute geodesic ring area."""
    area, _ = geod.geometry_area_perimeter(ring)
    if not math.isfinite(area):
        return 0.0
    return float(abs(area))


__all__ = [
    "CENTROID_KIND",
    "COORDINATE_PRECISION",
    "EARTH_RADIUS_M",
    "GeometryRejection",
    "PolygonGeometry",
    "compute_polygon_area_m2",
    "geometry_from_area",
    "geometry_from_geojson",
]
