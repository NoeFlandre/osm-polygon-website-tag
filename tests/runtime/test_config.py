"""Tests for typed config loading."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.runtime import config


def test_settings_defaults() -> None:
    settings = config.Settings()
    assert settings.github_repo == config.DEFAULT_GITHUB_REPO
    assert settings.hf_dataset_repo == config.DEFAULT_HF_DATASET
    assert settings.osm_poly_data_dir == ""


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_DATASET_REPO", "someone/else")
    settings = config.Settings()
    assert settings.hf_dataset_repo == "someone/else"


def test_the_settings_read_the_data_root_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", "/somewhere")
    assert config.Settings().osm_poly_data_dir == "/somewhere"
