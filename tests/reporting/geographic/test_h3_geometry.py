"""Tests for H3 coordinate and antimeridian geometry helpers."""

from __future__ import annotations

from typing import cast

import pytest

from osm_polygon_website_tag.reporting.geographic.h3_geometry import (
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
