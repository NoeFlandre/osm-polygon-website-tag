"""Coordinate validation and H3 boundary conversion."""

from __future__ import annotations

import math
from collections.abc import Sequence

import h3

from osm_polygon_website_tag.reporting.geographic.models import (
    DEFAULT_H3_RESOLUTION,
    GeographicMapError,
)


def assign_h3_cell(lat: float, lon: float, *, resolution: int = DEFAULT_H3_RESOLUTION) -> str:
    """Validate WGS84 coordinates and return their H3 cell."""
    _validate_resolution(resolution)
    _validate_coordinate(lat, lon)
    try:
        return h3.latlng_to_cell(lat, lon, resolution)
    except (TypeError, ValueError) as exc:
        raise GeographicMapError(f"invalid coordinate: lat={lat!r}, lon={lon!r}") from exc


def _validate_resolution(resolution: int) -> None:
    """Validate an H3 resolution accepted by the local h3 library."""
    if not isinstance(resolution, int) or isinstance(resolution, bool) or not 0 <= resolution <= 15:
        raise GeographicMapError(f"invalid H3 resolution: {resolution!r}")


def _validate_coordinate(lat: float, lon: float) -> None:
    """Validate finite WGS84 latitude and longitude bounds."""
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise GeographicMapError(f"non-finite coordinate: lat={lat!r}, lon={lon!r}")
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise GeographicMapError(f"coordinate out of range: lat={lat!r}, lon={lon!r}")


def cell_boundary_rings(cell_id: str) -> list[list[tuple[float, float]]]:
    """Return H3 boundary rings as ``(longitude, latitude)`` coordinates."""
    boundary = h3.cell_to_boundary(cell_id)
    points = [(float(lon), float(lat)) for lat, lon in boundary]
    return split_antimeridian(points)


def split_antimeridian(points: Sequence[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Clip a longitude-crossing ring into local world-coordinate rings."""
    if _ring_is_short_or_local(points):
        return [list(points)]
    unwrapped = _unwrap_ring(points)
    rings: list[list[tuple[float, float]]] = []
    for slab in _slab_range(unwrapped):
        clipped = _clip_ring_to_slab(unwrapped, slab)
        if len(clipped) >= 3:
            rings.append(_normalise_slab_ring(clipped, slab))
    return rings


def _ring_is_short_or_local(points: Sequence[tuple[float, float]]) -> bool:
    """Return whether a ring needs no antimeridian clipping."""
    return len(points) < 3 or all(
        abs(points[index][0] - points[index - 1][0]) <= 180.0 for index in range(len(points))
    )


def _unwrap_ring(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Unwrap successive longitudes so a crossing ring is continuous."""
    unwrapped = [points[0]]
    for longitude, latitude in points[1:]:
        previous_longitude = unwrapped[-1][0]
        while longitude - previous_longitude > 180.0:
            longitude -= 360.0
        while longitude - previous_longitude < -180.0:
            longitude += 360.0
        unwrapped.append((longitude, latitude))
    return unwrapped


def _slab_range(points: Sequence[tuple[float, float]]) -> range:
    """Return the world-width longitude slabs intersecting an unwrapped ring."""
    longitudes = [longitude for longitude, _ in points]
    minimum = math.floor((min(longitudes) + 180.0) / 360.0)
    maximum = math.floor((max(longitudes) + 180.0) / 360.0)
    return range(minimum, maximum + 1)


def _clip_ring_to_slab(
    points: Sequence[tuple[float, float]], slab: int
) -> list[tuple[float, float]]:
    """Clip an unwrapped ring to one world-width longitude slab."""
    left = -180.0 + 360.0 * slab
    right = 180.0 + 360.0 * slab
    return _clip_longitude(
        _clip_longitude(points, left, keep_greater=True), right, keep_greater=False
    )


def _normalise_slab_ring(
    points: Sequence[tuple[float, float]], slab: int
) -> list[tuple[float, float]]:
    """Shift a clipped slab ring back into the conventional longitude range."""
    return [(longitude - 360.0 * slab, latitude) for longitude, latitude in points]


def _clip_longitude(
    points: Sequence[tuple[float, float]],
    boundary: float,
    *,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    """Clip a ring against one vertical longitude boundary."""
    if not points:
        return []

    output: list[tuple[float, float]] = []
    previous = points[-1]
    for current in points:
        output.extend(_clip_edge(previous, current, boundary, keep_greater=keep_greater))
        previous = current
    return output


def _clip_edge(
    previous: tuple[float, float],
    current: tuple[float, float],
    boundary: float,
    *,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    """Return the part of one polygon edge retained by a clip boundary."""
    previous_inside = _inside(previous, boundary, keep_greater)
    current_inside = _inside(current, boundary, keep_greater)
    if current_inside:
        crossing = [] if previous_inside else [_intersection(previous, current, boundary)]
        return [*crossing, current]
    if previous_inside:
        return [_intersection(previous, current, boundary)]
    return []


def _inside(point: tuple[float, float], boundary: float, keep_greater: bool) -> bool:
    """Return whether a point is on the kept side of a longitude boundary."""
    return point[0] >= boundary if keep_greater else point[0] <= boundary


def _intersection(
    start: tuple[float, float], end: tuple[float, float], boundary: float
) -> tuple[float, float]:
    """Intersect a segment with a vertical longitude boundary."""
    delta = end[0] - start[0]
    if delta == 0.0:
        return boundary, start[1]
    ratio = (boundary - start[0]) / delta
    return boundary, start[1] + ratio * (end[1] - start[1])
