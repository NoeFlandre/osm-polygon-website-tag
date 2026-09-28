"""Contracts keeping the reference docs aligned with the code they describe."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "osm_polygon_website_tag"


def test_cli_reference_lists_every_command() -> None:
    import typer.main

    from osm_polygon_website_tag.application.cli import app

    commands = set(typer.main.get_command(app).commands)  # ty: ignore[unresolved-attribute]
    reference = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    table = reference.split("## Commands", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE))

    assert commands
    assert commands == documented


@pytest.mark.parametrize(
    "package", ["application", "contracts", "pipeline", "reporting", "publishing"]
)
def test_package_readme_names_every_module(package: str) -> None:
    readme = (PACKAGE / package / "README.md").read_text(encoding="utf-8")
    modules = {path.stem for path in (PACKAGE / package).glob("*.py") if path.stem != "__init__"}

    missing = sorted(module for module in modules if f"`{module}`" not in readme)

    assert missing == []


def test_every_navigation_target_exists() -> None:
    import yaml

    nav = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))["nav"]
    targets = [target for entry in nav for target in entry.values()]

    assert targets
    assert [target for target in targets if not (ROOT / "docs" / target).is_file()] == []
