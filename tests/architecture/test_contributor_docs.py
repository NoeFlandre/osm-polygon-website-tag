"""Contributor-facing files exist and are linked (#88)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_contributing_and_security_exist_and_are_linked_from_the_readme() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for name in ("CONTRIBUTING.md", "SECURITY.md"):
        assert (ROOT / name).is_file()
        assert f"]({name})" in readme


def test_contributing_names_every_gate_tier() -> None:
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")

    for recipe in ("focused", "qa-push", "qa-pr", "qa-merge", "mutation-scope"):
        assert f"just {recipe}" in contributing
        assert f"\n{recipe}" in justfile


def test_security_points_to_private_reporting() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "/security/advisories/new" in security


def test_issue_forms_and_pr_template_are_valid() -> None:
    for name in ("bug.yml", "feature.yml"):
        form = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text())
        assert form["name"]
        assert form["body"]
    template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    assert "just qa-pr" in template
