"""Contracts keeping the reference docs aligned with the code they describe."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "osm_polygon_website_tag"


def _navigation_targets(value: object) -> list[str]:
    """Return documentation paths from flat or nested MkDocs navigation."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [target for item in value for target in _navigation_targets(item)]
    if isinstance(value, dict):
        return [target for item in value.values() for target in _navigation_targets(item)]
    return []


def test_cli_reference_lists_every_command() -> None:
    import typer.main

    from osm_polygon_website_tag.application.cli import app

    commands = set(typer.main.get_command(app).commands)  # ty: ignore[unresolved-attribute]
    reference = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    table = reference.split("## Commands", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE))

    assert commands
    assert commands == documented


def test_cli_package_modules_stay_below_the_size_budget() -> None:
    cli_package = PACKAGE / "application" / "cli"
    modules = sorted(cli_package.glob("*.py"))

    assert {path.stem for path in modules} == {
        "__init__",
        "__main__",
        "_common",
        "grid5000",
        "languages",
        "publish",
        "run",
        "sentences",
        "verify",
    }
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in modules
        if len(path.read_text(encoding="utf-8").splitlines()) > 250
    }

    assert oversized == {}


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
    targets = _navigation_targets(nav)

    assert targets
    assert [target for target in targets if not (ROOT / "docs" / target).is_file()] == []
