"""Behavior of the offline basemap renderer."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from matplotlib.colors import to_rgba

from osm_polygon_website_tag.reporting.geographic import basemap


def test_matplotlib_patches_reuses_a_cached_module(monkeypatch: pytest.MonkeyPatch) -> None:
    cached = object()
    monkeypatch.setitem(basemap.__dict__, "patches", cached)

    assert basemap._matplotlib_patches() is cached


def test_matplotlib_patches_loads_and_caches_the_headless_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_patches = ModuleType("matplotlib.patches")
    fake_matplotlib = ModuleType("matplotlib")
    backend_calls: list[str] = []
    fake_matplotlib_any = cast(Any, fake_matplotlib)
    fake_matplotlib_any.use = backend_calls.append
    fake_matplotlib_any.patches = fake_patches
    monkeypatch.setitem(sys.modules, "matplotlib", fake_matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.patches", fake_patches)
    monkeypatch.delitem(basemap.__dict__, "patches", raising=False)

    assert basemap._matplotlib_patches() is fake_patches
    assert basemap.__dict__["patches"] is fake_patches
    assert backend_calls == ["Agg"]


def test_draw_landmasses_rejects_a_missing_backdrop(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="missing bundled land backdrop"):
        basemap.draw_landmasses(SimpleNamespace(), tmp_path / "missing.geojson")


class _Axis:
    def __init__(self) -> None:
        self.patches: list[Any] = []

    def add_patch(self, patch: Any) -> None:
        self.patches.append(patch)


def _draw(tmp_path: Path, document: dict[str, object]) -> list[Any]:
    path = tmp_path / "land.geojson"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    axis = _Axis()
    basemap.draw_landmasses(axis, path)
    return axis.patches


OUTER = [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
HOLE = [[0.2, 0.2], [0.3, 0.2], [0.2, 0.3]]


def test_draw_landmasses_draws_polygons_multipolygons_and_holes(tmp_path: Path) -> None:
    features = [
        {
            "type": "Feature",
            "properties": {"name": "Île-de-France"},
            "geometry": {"type": "Polygon", "coordinates": [OUTER, HOLE]},
        },
        {
            "type": "Feature",
            "geometry": {"type": "MultiPolygon", "coordinates": [[OUTER], [OUTER]]},
        },
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": OUTER}},
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}},
        {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": []}},
        {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [[]]}},
        {"type": "Feature"},
    ]

    patches = _draw(tmp_path, {"features": features})

    land = to_rgba(basemap.LAND_COLOR)
    ocean = to_rgba(basemap.OCEAN_COLOR)
    assert [(patch.get_facecolor(), patch.get_zorder()) for patch in patches] == [
        (land, 1),
        (ocean, 2),
        (land, 1),
        (land, 1),
    ]
    assert all(patch.get_edgecolor() == to_rgba(basemap.LAND_EDGE_COLOR) for patch in patches)
    assert all(patch.get_linewidth() == 0.3 for patch in patches)
    assert all(patch.get_closed() is True for patch in patches)
    assert patches[1].get_xy()[:3].tolist() == HOLE


def test_draw_landmasses_handles_a_feature_collection_without_features(tmp_path: Path) -> None:
    assert _draw(tmp_path, {"type": "FeatureCollection"}) == []


def test_draw_landmasses_draws_the_bundled_backdrop() -> None:
    axis = _Axis()

    basemap.draw_landmasses(axis)

    assert len(axis.patches) > 100


def test_a_faked_matplotlib_import_does_not_outlive_its_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pollute both lazy caches through the production loaders themselves.

    Paired with the assertion below: the loader writes the fake into module
    globals, which `monkeypatch` cannot undo, so only the package fixture
    keeps the fake from reaching the next test.
    """
    fake_patches = ModuleType("matplotlib.patches")
    fake_matplotlib = ModuleType("matplotlib")
    fake_matplotlib_any = cast(Any, fake_matplotlib)
    fake_matplotlib_any.use = lambda _backend: None
    fake_matplotlib_any.patches = fake_patches
    monkeypatch.setitem(sys.modules, "matplotlib", fake_matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.patches", fake_patches)
    monkeypatch.delitem(basemap.__dict__, "patches", raising=False)

    assert basemap._matplotlib_patches() is fake_patches
    assert basemap.__dict__["patches"] is fake_patches


def test_the_real_patches_module_survives_a_polluting_test() -> None:
    """The previous test's fake must not be what this one resolves."""
    patches_module = basemap._matplotlib_patches()

    assert hasattr(patches_module, "Polygon")
    assert basemap.__dict__["patches"] is patches_module
