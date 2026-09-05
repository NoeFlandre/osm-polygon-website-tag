"""Tests for path resolution. Keep these hermetic: no real external drive access."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_website_tag.runtime import paths
from osm_polygon_website_tag.runtime.paths import (
    assert_seagate_path,
    glotlid_model_cache_dir,
)


def test_default_data_root_is_the_project_storage_root() -> None:
    assert Path("/Volumes/Seagate M3/projects/osm-polygon-website-tag") == paths.DEFAULT_DATA_ROOT


def test_assert_seagate_path_accepts_the_project_storage_root() -> None:
    path = paths.DEFAULT_DATA_ROOT / "runs" / "example"

    assert assert_seagate_path(path, label="run") == path


def test_assert_seagate_path_keeps_legacy_root_for_existing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_root = tmp_path / "legacy-data"
    monkeypatch.setattr(paths, "LEGACY_DATA_ROOT", legacy_root)
    path = legacy_root / "runs" / "existing"

    assert assert_seagate_path(path, label="run") == path


def test_glotlid_model_cache_is_under_the_default_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OSM_POLY_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "DEFAULT_DATA_ROOT", tmp_path)
    assert glotlid_model_cache_dir() == paths.DEFAULT_DATA_ROOT / "models" / "glotlid"


def test_glotlid_model_cache_rejects_external_override_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "DEFAULT_DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(paths, "LEGACY_DATA_ROOT", tmp_path / "legacy-data")
    external_root = tmp_path / "external"
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(external_root))

    with pytest.raises(ValueError, match="Seagate data root"):
        glotlid_model_cache_dir()

    assert not external_root.exists()


def test_assert_seagate_path_rejects_paths_outside_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "DEFAULT_DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(paths, "LEGACY_DATA_ROOT", tmp_path / "legacy-data")
    with pytest.raises(ValueError, match="Seagate data root"):
        assert_seagate_path(tmp_path, label="model cache")


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force the data root to a temp directory for the duration of a test."""
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(tmp_path))
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
