"""Contracts for the renderer's lazy Matplotlib boundary."""

from __future__ import annotations

import builtins
import sys
from types import ModuleType
from typing import Any, cast

import pytest

from osm_polygon_website_tag.reporting.geographic import rendering


def test_matplotlib_components_reuses_a_complete_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    components = (object(), object(), object())
    monkeypatch.setitem(rendering.__dict__, "colors", components[0])
    monkeypatch.setitem(rendering.__dict__, "patches", components[1])
    monkeypatch.setitem(rendering.__dict__, "plt", components[2])

    def unexpected_import(name: str, *args: object, **kwargs: object) -> ModuleType:
        del args, kwargs
        raise AssertionError(f"unexpected Matplotlib import: {name}")

    monkeypatch.setattr(builtins, "__import__", unexpected_import)

    assert rendering._matplotlib_components() == components


@pytest.mark.parametrize("missing", ["colors", "patches", "plt"])
def test_matplotlib_components_repairs_a_partial_cache(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    fake_colors = ModuleType("matplotlib.colors")
    fake_patches = ModuleType("matplotlib.patches")
    fake_pyplot = ModuleType("matplotlib.pyplot")
    fake_matplotlib = ModuleType("matplotlib")
    backend_calls: list[str] = []
    fake_matplotlib_any = cast(Any, fake_matplotlib)
    fake_matplotlib_any.use = backend_calls.append
    fake_matplotlib_any.colors = fake_colors
    fake_matplotlib_any.patches = fake_patches
    fake_matplotlib_any.pyplot = fake_pyplot
    for name, module in (
        ("matplotlib", fake_matplotlib),
        ("matplotlib.colors", fake_colors),
        ("matplotlib.patches", fake_patches),
        ("matplotlib.pyplot", fake_pyplot),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(rendering.__dict__, "colors", fake_colors)
    monkeypatch.setitem(rendering.__dict__, "patches", fake_patches)
    monkeypatch.setitem(rendering.__dict__, "plt", fake_pyplot)
    monkeypatch.setitem(rendering.__dict__, missing, None)

    assert rendering._matplotlib_components() == (fake_colors, fake_patches, fake_pyplot)
    assert backend_calls == ["Agg"]
    assert rendering.__dict__["colors"] is fake_colors
    assert rendering.__dict__["patches"] is fake_patches
    assert rendering.__dict__["plt"] is fake_pyplot


def test_module_compatibility_attributes_delegate_to_the_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    components = (object(), object(), object())
    monkeypatch.setattr(rendering, "_matplotlib_components", lambda: components)

    assert rendering.__getattr__("colors") is components[0]
    assert rendering.__getattr__("patches") is components[1]
    assert rendering.__getattr__("plt") is components[2]


def test_unknown_module_attribute_has_a_normal_attribute_error() -> None:
    with pytest.raises(
        AttributeError,
        match=r"^module 'osm_polygon_website_tag\.reporting\.geographic\.rendering' has no attribute 'missing'$",
    ):
        rendering.__getattr__("missing")


def test_unknown_module_attribute_does_not_load_matplotlib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rendering,
        "_matplotlib_components",
        lambda: pytest.fail("unknown attributes must not load Matplotlib"),
    )

    with pytest.raises(AttributeError):
        rendering.__getattr__("unknown")
