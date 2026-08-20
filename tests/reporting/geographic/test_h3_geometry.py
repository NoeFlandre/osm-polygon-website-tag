"""Tests for H3 coordinate and antimeridian geometry helpers."""

from __future__ import annotations

from typing import cast

import pytest

from osm_polygon_website_tag.reporting.geographic.h3_geometry import (
    _clip_edge,
    _clip_longitude,
    _clip_ring_to_slab,
    _inside,
    _intersection,
    _normalise_slab_ring,
    _ring_is_short_or_local,
    _slab_range,
    _unwrap_ring,
    assign_h3_cell,
    split_antimeridian,
)
from osm_polygon_website_tag.reporting.geographic.models import GeographicMapError


def test_split_antimeridian_returns_closed_local_rings() -> None:
    rings = split_antimeridian([(179.0, 0.0), (-179.0, 0.0), (-179.0, 1.0), (179.0, 1.0)])

    assert len(rings) == 2
    assert all(len(ring) >= 3 for ring in rings)
    assert all(
        max(point[0] for point in ring) - min(point[0] for point in ring) <= 180 for ring in rings
    )


@pytest.mark.parametrize("resolution", [-1, 16, True])
def test_assign_h3_cell_rejects_invalid_resolutions(resolution: object) -> None:
    with pytest.raises(GeographicMapError, match="invalid H3 resolution"):
        assign_h3_cell(0.0, 0.0, resolution=cast(int, resolution))


def test_h3_clipping_helpers_handle_edges_and_boundaries() -> None:
    points = [(179.0, 0.0), (181.0, 1.0), (181.0, 2.0)]
    assert not _ring_is_short_or_local([(179.0, 0.0), (-179.0, 0.0), (179.0, 1.0)])
    assert _unwrap_ring([(179.0, 0.0), (-179.0, 1.0)]) == [(179.0, 0.0), (181.0, 1.0)]
    assert list(_slab_range(points)) == [0, 1]
    assert _inside((0.0, 1.0), 0.0, True)
    assert not _inside((-1.0, 1.0), 0.0, True)
    assert _intersection((0.0, 0.0), (2.0, 2.0), 1.0) == (1.0, 1.0)
    assert _intersection((1.0, 2.0), (1.0, 3.0), 1.0) == (1.0, 2.0)
    assert _clip_edge((0.0, 0.0), (2.0, 2.0), 1.0, keep_greater=True) == [(1.0, 1.0), (2.0, 2.0)]
    assert _clip_longitude(points, 180.0, keep_greater=True)
    assert _clip_ring_to_slab(points, 0)
    assert _normalise_slab_ring([(360.0, 1.0), (361.0, 2.0), (360.0, 1.0)], 1) == [
        (0.0, 1.0),
        (1.0, 2.0),
        (0.0, 1.0),
    ]
