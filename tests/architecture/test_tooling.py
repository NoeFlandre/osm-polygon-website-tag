"""Contracts keeping local and CI quality tooling aligned."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _implicit_text_io_calls() -> list[str]:
    violations: list[str] = []
    for source in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"read_text", "write_text"}:
                continue
            if not any(
                keyword.arg == "encoding"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "utf-8"
                for keyword in node.keywords
            ):
                violations.append(f"{source}:{node.lineno}:{node.func.attr}")
    return violations


def test_requested_python_tools_are_direct_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    runtime = "\n".join(project["project"]["dependencies"])
    development = "\n".join(project["dependency-groups"]["dev"])

    for package in ("rich", "tqdm", "typer"):
        assert re.search(rf"(?m)^{package}[<>=]", runtime)
    for package in ("pre-commit", "pytest", "ruff", "ty"):
        assert re.search(rf"(?m)^{package}[<>=]", development)


def test_justfile_exposes_canonical_quality_recipes() -> None:
    justfile = (ROOT / "justfile").read_text()

    for recipe in (
        "sync:",
        "lock:",
        "lint:",
        "format:",
        "format-check:",
        "typecheck:",
        "test:",
        "build:",
        "check:",
        "pre-commit:",
        "pre-push:",
        "install-hooks:",
        "docker-build:",
        "docker-smoke:",
    ):
        assert recipe in justfile
    for command in (
        "uv lock --check",
        "uv run --locked ruff check .",
        "uv run --locked ruff format --check .",
        "uv run --locked ty check src tests",
        "uv run --locked pytest",
        "uv build",
        "docker build --pull",
        "docker run --rm --read-only",
    ):
        assert command in justfile


def test_pre_commit_uses_uv_locked_project_tools() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text()

    assert "repo: local" in config
    assert "uv run --locked ruff check --fix" in config
    assert "uv run --locked ruff format" in config
    assert "uv run --locked ty check src tests" in config
    assert "uv run --locked pytest" in config
    assert "stages: [pre-push]" in config


def test_github_actions_is_read_only_pinned_and_runs_just() -> None:
    workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text()

    assert "contents: read" in workflow
    assert "uv sync --locked" in workflow
    assert "run: just check" in workflow
    assert "HF_TOKEN" not in workflow
    uses = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow)
    assert len(uses) == 3
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)


def test_production_text_io_declares_utf8_encoding() -> None:
    assert _implicit_text_io_calls() == []
