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


def _nested_first_party_imports() -> list[str]:
    violations: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function_depth = 0
            self.violations: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        def visit_Import(self, node: ast.Import) -> None:
            if self.function_depth:
                self.violations.extend(
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("osm_polygon_website_tag.")
                    or alias.name == "osm_polygon_website_tag"
                )

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            if self.function_depth and (
                module.startswith("osm_polygon_website_tag.") or module == "osm_polygon_website_tag"
            ):
                self.violations.append(module)

    for source in sorted((ROOT / "src").rglob("*.py")):
        visitor = Visitor()
        visitor.visit(ast.parse(source.read_text(encoding="utf-8"), filename=str(source)))
        violations.extend(f"{source}:{name}" for name in visitor.violations)
    return sorted(violations)


def test_requested_python_tools_are_direct_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    runtime = "\n".join(project["project"]["dependencies"])
    development = "\n".join(project["dependency-groups"]["dev"])

    for package in ("rich", "tqdm", "typer"):
        assert re.search(rf"(?m)^{package}[<>=]", runtime)
    for package in ("mutmut", "pre-commit", "pytest", "radon", "ruff", "ty"):
        assert re.search(rf"(?m)^{package}[<>=]", development)


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


def test_github_actions_are_read_only_and_sha_pinned() -> None:
    """Workflows get read-only tokens, no secrets, and SHA-pinned actions."""
    for name in ("quality.yml", "mutation-sweep.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text()

        assert "contents: read" in workflow, name
        assert "HF_TOKEN" not in workflow, name
        uses = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow)
        assert uses, name
        assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses), name


def test_production_text_io_declares_utf8_encoding() -> None:
    assert _implicit_text_io_calls() == []


def test_production_code_does_not_defer_first_party_imports() -> None:
    assert _nested_first_party_imports() == []
