"""Pinned ecosystems are kept current and dependencies are bounded (#71)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_dependabot_updates_every_pinned_ecosystem_weekly_and_grouped() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    updates = config["updates"]

    assert {update["package-ecosystem"] for update in updates} == {"github-actions", "docker", "uv"}
    assert all(update["schedule"]["interval"] == "weekly" for update in updates)
    assert all(update.get("groups") for update in updates)


def test_every_runtime_and_dev_dependency_has_an_upper_bound() -> None:
    project = _project()
    pins = [
        *project["project"]["dependencies"],
        *project["project"]["optional-dependencies"]["sentences"],
        *project["dependency-groups"]["dev"],
    ]

    unbounded = [
        pin
        for pin in pins
        if not any(spec.operator in {"<", "<=", "=="} for spec in Requirement(pin).specifier)
    ]

    assert unbounded == []


def test_pre_release_tools_stay_on_their_tested_series() -> None:
    dev = _project()["dependency-groups"]["dev"]

    assert "ty>=0.0.65,<0.1" in dev
    assert "ruff>=0.5,<0.17" in dev
