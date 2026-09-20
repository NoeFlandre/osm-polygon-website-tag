"""Focused contracts for the offline basemap renderer."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

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


def test_draw_landmasses_reads_features_and_delegates_in_order(tmp_path, monkeypatch) -> None:
    path = tmp_path / "land.geojson"
    features = [
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}},
        {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": []}},
    ]
    path.write_text(json.dumps({"features": features}), encoding="utf-8")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(basemap, "_draw_feature", lambda _axis, feature: calls.append(feature))

    axis = object()
    basemap.draw_landmasses(axis, path)

    assert calls == features


def test_draw_landmasses_handles_a_feature_collection_without_features(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "empty.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection"}), encoding="utf-8")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(basemap, "_draw_feature", lambda _axis, feature: calls.append(feature))

    basemap.draw_landmasses(object(), path)

    assert calls == []


def test_draw_landmasses_reads_utf8_json_with_the_declared_encoding(tmp_path, monkeypatch) -> None:
    path = tmp_path / "unicode.geojson"
    feature = {
        "type": "Feature",
        "properties": {"name": "Île-de-France"},
        "geometry": {"type": "Polygon", "coordinates": []},
    }
    path.write_text(json.dumps({"features": [feature]}, ensure_ascii=False), encoding="utf-8")
    calls: list[dict[str, object]] = []
    encodings: list[str | None] = []
    original_read_text = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        encodings.append(encoding)
        return original_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(basemap, "_draw_feature", lambda _axis, received: calls.append(received))

    basemap.draw_landmasses(object(), path)

    assert encodings == ["utf-8"]
    assert calls == [feature]


def test_draw_feature_dispatches_only_supported_geometry_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        basemap,
        "_draw_polygon_feature",
        lambda _axis, coordinates: calls.append(("polygon", coordinates)),
    )
    monkeypatch.setattr(
        basemap,
        "_draw_multipolygon_feature",
        lambda _axis, coordinates: calls.append(("multipolygon", coordinates)),
    )

    axis = object()
    basemap._draw_feature(axis, {"geometry": {"type": "Polygon", "coordinates": [1]}})
    basemap._draw_feature(axis, {"geometry": {"type": "MultiPolygon", "coordinates": [2]}})
    basemap._draw_feature(axis, {"geometry": {"type": "LineString", "coordinates": [3]}})
    basemap._draw_feature(axis, {})

    assert calls == [("polygon", [1]), ("multipolygon", [2])]


def test_draw_polygon_emits_outer_ring_and_holes_with_distinct_styles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Polygon:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

    fake_patches = SimpleNamespace(Polygon=Polygon)
    monkeypatch.setitem(basemap.__dict__, "patches", fake_patches)
    outer = [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    hole = [[0.2, 0.2], [0.3, 0.2], [0.2, 0.3]]
    axis = SimpleNamespace(add_patch=lambda patch: None)
    added: list[object] = []
    axis.add_patch = added.append

    basemap._draw_polygon(axis, [outer, hole])
    basemap._draw_polygon(axis, [])

    assert len(added) == 2
    assert calls == [
        (
            (outer,),
            {
                "closed": True,
                "facecolor": basemap.LAND_COLOR,
                "edgecolor": basemap.LAND_EDGE_COLOR,
                "linewidth": 0.3,
                "zorder": 1,
            },
        ),
        (
            (hole,),
            {
                "closed": True,
                "facecolor": basemap.OCEAN_COLOR,
                "edgecolor": basemap.LAND_EDGE_COLOR,
                "linewidth": 0.3,
                "zorder": 2,
            },
        ),
    ]


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
