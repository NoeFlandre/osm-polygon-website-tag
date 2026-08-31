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
    for package in ("mutmut", "pre-commit", "pytest", "radon", "ruff", "ty"):
        assert re.search(rf"(?m)^{package}[<>=]", development)


def test_justfile_exposes_canonical_quality_recipes() -> None:
    justfile = (ROOT / "justfile").read_text()

    for recipe in (
        "sync:",
        "lock:",
        "baseline:",
        "lint:",
        "ruff:",
        "format:",
        "format-check:",
        "unit:",
        "acceptance:",
        "architecture:",
        "typecheck:",
        "test:",
        "build:",
        "check:",
        "smoke:",
        "diff-review:",
        "qa-gauntlet:",
        "pre-commit:",
        "pre-push:",
        "install-hooks:",
        "docker-build:",
        "docker-smoke:",
        "coverage:",
        "crap:",
        "mutation:",
        "quality:",
    ):
        assert recipe in justfile
    for command in (
        "uv lock --check",
        "uv run --locked ruff check .",
        "uv run --locked ruff format --check .",
        "uv run --locked pytest tests --ignore=tests/acceptance --ignore=tests/architecture",
        "uv run --locked pytest tests/acceptance",
        "uv run --locked pytest tests/architecture",
        "uv run --locked ty check src tests scripts",
        "uv run --locked pytest",
        "uv build",
        "git diff --check",
        "docker build --pull",
        "docker run --rm --read-only",
    ):
        assert command in justfile
    gauntlet = re.search(r"^qa-gauntlet:\s*(.*)$", justfile, re.MULTILINE)
    assert gauntlet is not None
    assert gauntlet.group(1).strip() == (
        "baseline ruff typecheck unit acceptance architecture crap mutation smoke diff-review"
    )
    assert "--max-crap 6" in justfile
    assert "--path src/osm_polygon_website_tag" in justfile
    assert "--path src/osm_polygon_website_tag/application/workflow.py" not in justfile
    assert "python scripts/quality/mutation_runner.py" in justfile


def test_justfile_keeps_uv_cache_on_seagate_when_available() -> None:
    justfile = (ROOT / "justfile").read_text()

    assert "set export" in justfile
    assert 'UV_CACHE_DIR := if env("UV_CACHE_DIR", "") != "" {' in justfile
    assert 'path_exists("/Volumes/Seagate M3/projects/osm-polygon-website-tag")' in justfile
    assert '"/Volumes/Seagate M3/projects/osm-polygon-website-tag/uv-cache"' in justfile


def test_justfile_keeps_build_artifacts_on_seagate_when_available() -> None:
    justfile = (ROOT / "justfile").read_text()

    assert (
        'BUILD_OUTPUT_DIR := if path_exists("/Volumes/Seagate M3/projects/osm-polygon-website-tag")'
        in justfile
    )
    assert '"/Volumes/Seagate M3/projects/osm-polygon-website-tag/build"' in justfile
    assert 'uv build --out-dir "{{ BUILD_OUTPUT_DIR }}"' in justfile


def test_mutation_gate_covers_the_whole_package_and_behavior_suite() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    config = project["tool"]["mutmut"]

    assert config["source_paths"] == ["src/osm_polygon_website_tag"]
    assert "pytest_add_cli_args_test_selection" not in config
    assert "--ignore=tests/architecture" in config["pytest_add_cli_args"]
    assert {
        ".github",
        ".pre-commit-config.yaml",
        ".dockerignore",
        "Dockerfile",
        "LICENSE",
        "README.md",
        "docs",
        "justfile",
        "mkdocs.yml",
        "scripts",
    } <= set(config["also_copy"])


def test_pre_commit_uses_uv_locked_project_tools() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text()

    assert "repo: local" in config
    assert "uv run --locked ruff check --fix" in config
    assert "uv run --locked ruff format" in config
    assert "uv run --locked ty check src tests scripts" in config
    assert "uv run --locked pytest" in config
    assert "stages: [pre-push]" in config


def test_github_actions_is_read_only_pinned_and_runs_just() -> None:
    workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text()

    assert "contents: read" in workflow
    assert "uv sync --locked" in workflow
    assert "run: just qa-gauntlet" in workflow
    assert "HF_TOKEN" not in workflow
    uses = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow)
    assert len(uses) == 3
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)


def test_production_text_io_declares_utf8_encoding() -> None:
    assert _implicit_text_io_calls() == []
