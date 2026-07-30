"""Tests for path resolution. Keep these hermetic: no real external drive access."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_website_tag.runtime import paths


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force the data root to a temp directory for the duration of a test."""
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(tmp_path))
    # Clear the lru_cache if we add one later; safe to call even without one.
    return tmp_path


def test_data_root_uses_env_override(isolated_data_dir: Path) -> None:
    assert paths.data_root() == isolated_data_dir


def test_subdirs_are_created(isolated_data_dir: Path) -> None:
    assert paths.raw_dir() == isolated_data_dir / paths.RAW_DIRNAME
    assert paths.processed_dir() == isolated_data_dir / paths.PROCESSED_DIRNAME
    assert paths.exports_dir() == isolated_data_dir / paths.EXPORTS_DIRNAME
    assert paths.exports_dir().is_dir()


def test_default_data_dir_is_dedicated_seagate_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OSM_POLY_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "DEFAULT_DATA_ROOT", tmp_path)
    assert paths.data_root() == tmp_path
