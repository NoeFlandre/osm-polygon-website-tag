"""Tests for H3 coordinate and antimeridian geometry helpers."""

from __future__ import annotations

from osm_polygon_website_tag.reporting.geographic.h3_geometry import split_antimeridian


def test_split_antimeridian_returns_closed_local_rings() -> None:
    rings = split_antimeridian([(179.0, 0.0), (-179.0, 0.0), (-179.0, 1.0), (179.0, 1.0)])

    assert len(rings) == 2
    assert all(len(ring) >= 3 for ring in rings)
    assert all(
        max(point[0] for point in ring) - min(point[0] for point in ring) <= 180 for ring in rings
    )
