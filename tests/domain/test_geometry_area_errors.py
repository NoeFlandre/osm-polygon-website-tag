"""Area errors of ``compute_polygon_area_m2``: degenerate rings, GeodError, real bugs."""

from __future__ import annotations

import pyproj.exceptions
import pytest

from osm_polygon_website_tag.domain import geometry as geometry_module
from osm_polygon_website_tag.domain.geometry import compute_polygon_area_m2


def test_a_degenerate_collinear_ring_has_zero_area() -> None:
    assert compute_polygon_area_m2([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [0.0, 0.0]]) == 0.0


@pytest.mark.parametrize("error", [pyproj.exceptions.GeodError("geod"), ValueError("bad ring")])
def test_a_ring_pyproj_cannot_measure_counts_as_zero_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def fail(*_args: object) -> float:
        raise error

    monkeypatch.setattr(geometry_module, "_finite_abs_area", fail)

    with caplog.at_level("WARNING", logger="osm_polygon_website_tag.domain.geometry"):
        area = compute_polygon_area_m2([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]])

    assert area == 0.0
    assert [record.name for record in caplog.records] == [geometry_module.__name__]
    assert caplog.messages == [f"geodesic area failed for a ring; counted as 0.0: {error}"]


def test_an_unexpected_area_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> float:
        raise RuntimeError("bug")

    monkeypatch.setattr(geometry_module, "_finite_abs_area", fail)

    with pytest.raises(RuntimeError, match="bug"):
        compute_polygon_area_m2([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]])
