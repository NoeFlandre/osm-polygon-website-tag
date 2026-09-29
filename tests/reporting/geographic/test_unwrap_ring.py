"""``_unwrap_ring`` makes a ring that crosses the antimeridian continuous."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.reporting.geographic.h3_geometry import _unwrap_ring


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        # An eastward crossing continues past +180; a westward one past -180.
        ([(170.0, 0.0), (-170.0, 1.0)], [(170.0, 0.0), (190.0, 1.0)]),
        ([(-170.0, 0.0), (170.0, 1.0)], [(-170.0, 0.0), (-190.0, 1.0)]),
        # Exactly half a world apart is not a crossing, in either direction.
        ([(0.0, 0.0), (180.0, 1.0)], [(0.0, 0.0), (180.0, 1.0)]),
        ([(0.0, 0.0), (-180.0, 1.0)], [(0.0, 0.0), (-180.0, 1.0)]),
        # One degree beyond half a world is.
        ([(0.0, 0.0), (181.0, 1.0)], [(0.0, 0.0), (-179.0, 1.0)]),
        ([(0.0, 0.0), (-181.0, 1.0)], [(0.0, 0.0), (179.0, 1.0)]),
        # Large longitudes that merely sum past 180 are left alone.
        ([(100.0, 0.0), (90.0, 1.0)], [(100.0, 0.0), (90.0, 1.0)]),
        # Several turns away come back in one step, at and just past the limits.
        ([(0.0, 0.0), (540.0, 1.0)], [(0.0, 0.0), (180.0, 1.0)]),
        ([(0.0, 0.0), (541.0, 1.0)], [(0.0, 0.0), (-179.0, 1.0)]),
        ([(0.0, 0.0), (-540.0, 1.0)], [(0.0, 0.0), (-180.0, 1.0)]),
        ([(0.0, 0.0), (-541.0, 1.0)], [(0.0, 0.0), (179.0, 1.0)]),
        ([(0.0, 0.0), (900.5, 1.0)], [(0.0, 0.0), (180.5 - 360.0, 1.0)]),
        ([(0.0, 0.0), (-900.5, 1.0)], [(0.0, 0.0), (360.0 - 180.5, 1.0)]),
        # Each step is unwrapped against the previous unwrapped point.
        (
            [(170.0, 0.0), (-170.0, 1.0), (-150.0, 2.0)],
            [(170.0, 0.0), (190.0, 1.0), (210.0, 2.0)],
        ),
    ],
)
def test_unwrap_ring_makes_a_crossing_ring_continuous(
    points: list[tuple[float, float]], expected: list[tuple[float, float]]
) -> None:
    assert _unwrap_ring(points) == expected
