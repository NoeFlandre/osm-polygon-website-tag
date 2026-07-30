"""Tests for typed config loading."""

from __future__ import annotations

from pathlib import Path

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


def test_resolved_data_root_returns_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", str(tmp_path))
    settings = config.Settings()
    assert settings.resolved_data_root() == str(tmp_path)
