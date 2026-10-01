"""The version lives in pyproject.toml only; every other copy must match it (#87)."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import osm_polygon_website_tag
from osm_polygon_website_tag.web.web_fetch import USER_AGENT

ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["version"]


def test_package_version_comes_from_the_installed_metadata() -> None:
    assert osm_polygon_website_tag.__version__ == version("osm-polygon-website-tag")
    assert osm_polygon_website_tag.__version__ == _pyproject_version()


def test_the_user_agent_carries_the_package_version() -> None:
    assert USER_AGENT.startswith(f"osm-polygon-website-tag/{_pyproject_version()} ")


def test_citation_and_bibtex_versions_match_the_package() -> None:
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    match = re.search(r"^version:\s*(\S+)\s*$", citation, re.MULTILINE)

    assert match is not None
    assert match.group(1) == _pyproject_version()
    assert f"version = {{{_pyproject_version()}}}" in readme


def test_changelog_has_unreleased_and_the_current_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [Unreleased]" in changelog
    assert f"## [{_pyproject_version()}]" in changelog


def test_the_release_workflow_checks_the_tag_against_the_version() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert '"v*"' in workflow
    assert 'if [ "${GITHUB_REF_NAME}" != "v${version}" ]' in workflow
    assert "uv build" in workflow
    assert "gh release create" in workflow
    uses = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow)
    assert uses and all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
