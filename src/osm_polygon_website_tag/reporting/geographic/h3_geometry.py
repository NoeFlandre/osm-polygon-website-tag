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
    if not isinstance(resolution, int) or isinstance(resolution, bool) or not 0 <= resolution <= 15:
        raise GeographicMapError(f"invalid H3 resolution: {resolution!r}")
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise GeographicMapError(f"non-finite coordinate: lat={lat!r}, lon={lon!r}")
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise GeographicMapError(f"coordinate out of range: lat={lat!r}, lon={lon!r}")
    try:
        return h3.latlng_to_cell(lat, lon, resolution)
    except (TypeError, ValueError) as exc:
        raise GeographicMapError(f"invalid coordinate: lat={lat!r}, lon={lon!r}") from exc


def cell_boundary_rings(cell_id: str) -> list[list[tuple[float, float]]]:
    """Return H3 boundary rings as ``(longitude, latitude)`` coordinates."""
    boundary = h3.cell_to_boundary(cell_id)
    points = [(float(lon), float(lat)) for lat, lon in boundary]
    return split_antimeridian(points)


def split_antimeridian(points: Sequence[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Clip a longitude-crossing ring into local world-coordinate rings."""
    if len(points) < 3:
        return [list(points)]
    if all(abs(points[index][0] - points[index - 1][0]) <= 180.0 for index in range(len(points))):
        return [list(points)]

    unwrapped = [points[0]]
    for longitude, latitude in points[1:]:
        previous_longitude = unwrapped[-1][0]
        while longitude - previous_longitude > 180.0:
            longitude -= 360.0
        while longitude - previous_longitude < -180.0:
            longitude += 360.0
        unwrapped.append((longitude, latitude))

    min_slab = math.floor((min(longitude for longitude, _ in unwrapped) + 180.0) / 360.0)
    max_slab = math.floor((max(longitude for longitude, _ in unwrapped) + 180.0) / 360.0)
    rings: list[list[tuple[float, float]]] = []
    for slab in range(min_slab, max_slab + 1):
        left = -180.0 + 360.0 * slab
        right = 180.0 + 360.0 * slab
        clipped = _clip_longitude(
            _clip_longitude(unwrapped, left, keep_greater=True),
            right,
            keep_greater=False,
        )
        if len(clipped) >= 3:
            rings.append([(longitude - 360.0 * slab, latitude) for longitude, latitude in clipped])
    return rings


def _clip_longitude(
    points: Sequence[tuple[float, float]],
    boundary: float,
    *,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    """Clip a ring against one vertical longitude boundary."""
    if not points:
        return []

    def inside(point: tuple[float, float]) -> bool:
        return point[0] >= boundary if keep_greater else point[0] <= boundary

    def intersection(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
        delta = end[0] - start[0]
        if delta == 0.0:
            return boundary, start[1]
        ratio = (boundary - start[0]) / delta
        return boundary, start[1] + ratio * (end[1] - start[1])

    output: list[tuple[float, float]] = []
    previous = points[-1]
    previous_inside = inside(previous)
    for current in points:
        current_inside = inside(current)
        if current_inside:
            if not previous_inside:
                output.append(intersection(previous, current))
            output.append(current)
        elif previous_inside:
            output.append(intersection(previous, current))
        previous = current
        previous_inside = current_inside
    return output
