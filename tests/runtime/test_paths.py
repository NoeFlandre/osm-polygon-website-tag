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


def test_assert_seagate_path_rejects_the_retired_legacy_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run now lives under the single project root."""
    monkeypatch.setattr(paths, "DEFAULT_DATA_ROOT", tmp_path / "osm-polygon-website-tag")

    with pytest.raises(ValueError, match="Seagate data root"):
        assert_seagate_path(tmp_path / "osm-polygon-website-tag-data" / "runs" / "old", label="run")


def test_the_data_root_is_the_single_project_directory() -> None:
    assert Path("/Volumes/Seagate M3/projects/osm-polygon-website-tag") == paths.DEFAULT_DATA_ROOT
    assert not hasattr(paths, "LEGACY_DATA_ROOT")


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
    external_root = tmp_path / "external"
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(external_root))

    with pytest.raises(ValueError, match="Seagate data root"):
        glotlid_model_cache_dir()

    assert not external_root.exists()


def test_assert_seagate_path_rejects_paths_outside_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "DEFAULT_DATA_ROOT", tmp_path / "data")
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


def test_sat_model_cache_lives_beside_the_other_model_caches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "Volumes" / "Seagate M3" / "projects" / "osm-polygon-website-tag"
    monkeypatch.setattr(paths, "_configured_data_root", lambda: root)
    monkeypatch.setattr(paths, "_is_under_seagate_root", lambda _path: True)

    cache = paths.sat_model_cache_dir()

    assert cache == root.resolve() / "models" / "sat"
    assert cache.is_dir()


def test_sat_model_cache_must_be_under_a_seagate_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths, "_configured_data_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "_is_under_seagate_root", lambda _path: False)

    with pytest.raises(ValueError, match="SaT model cache must be under a Seagate data root"):
        paths.sat_model_cache_dir()
