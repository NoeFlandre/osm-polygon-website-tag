"""Shared finalized-run fixtures for publication tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.reporting.test_finalize import _setup

from osm_polygon_website_tag.reporting.finalize import finalize_run


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    root, _state = _setup(tmp_path)
    assert finalize_run(root).ok
    return root
