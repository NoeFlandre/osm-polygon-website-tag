"""Fixtures for the geographic reporting tests."""

from __future__ import annotations

from collections.abc import Iterator
from types import ModuleType

import pytest

from osm_polygon_website_tag.reporting.geographic import basemap, rendering

# Both renderers load Matplotlib lazily and memoise it into their own module
# globals. A test that fakes the Matplotlib import does not write that fake
# itself -- the production loader does, on the first call -- so `monkeypatch`
# has no record of the entry and cannot undo it. The fake then outlives the
# test and every later one in the process resolves `patches` to an empty
# stand-in module, which fails only when the whole directory runs together.
_CACHED_COMPONENTS: tuple[tuple[ModuleType, tuple[str, ...]], ...] = (
    (basemap, ("patches",)),
    (rendering, ("colors", "patches", "plt")),
)


@pytest.fixture(autouse=True)
def _isolate_matplotlib_caches() -> Iterator[None]:
    """Restore the lazy Matplotlib caches around every test in this package."""
    saved = {
        (module, name): module.__dict__[name]
        for module, names in _CACHED_COMPONENTS
        for name in names
        if name in module.__dict__
    }
    try:
        yield
    finally:
        for module, names in _CACHED_COMPONENTS:
            for name in names:
                module.__dict__.pop(name, None)
        for (module, name), cached in saved.items():
            module.__dict__[name] = cached
