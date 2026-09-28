"""Tests for data-root resolution. Hermetic: no real external drive access."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_website_tag.runtime import paths
from osm_polygon_website_tag.runtime.paths import (
    model_cache_dir,
    require_under_data_root,
    resolve_data_root,
)


@pytest.fixture(autouse=True)
def _no_ambient_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from an empty directory with no data-root variable set."""
    monkeypatch.delenv(paths.DATA_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)


def test_the_environment_sets_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", f"  {tmp_path / 'root'}  ")

    assert resolve_data_root() == tmp_path / "root"


def test_a_dotenv_file_sets_the_data_root(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"OSM_POLY_DATA_DIR={tmp_path / 'from-env-file'}\n")

    assert resolve_data_root() == tmp_path / "from-env-file"


def test_the_unset_default_is_data_under_the_current_directory(tmp_path: Path) -> None:
    root = resolve_data_root()

    assert root == tmp_path.resolve() / "data"
    assert not str(root).startswith("/Volumes")
    assert not root.exists()


def test_a_home_relative_root_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OSM_POLY_DATA_DIR", "~/osm")

    assert resolve_data_root() == tmp_path.resolve() / "osm"


def test_the_root_is_resolved_to_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", "relative/root")

    assert resolve_data_root() == tmp_path.resolve() / "relative" / "root"


def test_a_model_cache_outside_any_seagate_volume_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(tmp_path / "x"))

    cache = model_cache_dir("glotlid")

    assert cache == tmp_path.resolve() / "x" / "models" / "glotlid"
    assert cache.is_dir()
    (cache / "marker").write_text("kept")
    assert model_cache_dir("glotlid") == cache
    assert (cache / "marker").read_text() == "kept"


def test_a_path_under_the_root_is_returned_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(tmp_path / "root"))

    assert require_under_data_root("root/runs/../runs/a", label="run") == (
        tmp_path.resolve() / "root" / "runs" / "a"
    )
    assert require_under_data_root(tmp_path / "root", label="run") == tmp_path.resolve() / "root"


def test_a_path_outside_the_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(tmp_path / "root"))

    with pytest.raises(ValueError) as caught:
        require_under_data_root(tmp_path / "rootless" / "run", label="run directory")

    assert str(caught.value) == (
        f"run directory must be under the data root {tmp_path.resolve() / 'root'} "
        "(set OSM_POLY_DATA_DIR)"
    )


def test_the_dead_layout_helpers_are_gone() -> None:
    for name in ("raw_dir", "processed_dir", "exports_dir", "data_root", "DEFAULT_DATA_ROOT"):
        assert not hasattr(paths, name)
