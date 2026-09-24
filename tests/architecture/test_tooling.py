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
        "mutation-scope",
        "mutation-gate *scopes",
        "mutation-module filters",
        "mutation-clean:",
        "quality:",
        "focused base=",
        "qa-push base=",
        "qa-pr:",
        "qa-merge:",
        "release-verify run_dir",
    ):
        assert recipe in justfile
    for command in (
        "uv lock --check",
        "uv run --locked ruff check .",
        "uv run --locked ruff format --check .",
        "uv run --locked pytest -n auto tests --ignore=tests/acceptance --ignore=tests/architecture",
        "uv run --locked pytest -n auto tests/acceptance",
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
    assert "python scripts/quality/mutation_scope.py" in justfile
    assert re.search(r"^qa-ci:", justfile, re.MULTILINE) is None
    ruff = re.search(r"^ruff:[ \t]*(.*)$", justfile, re.MULTILINE)
    assert ruff is not None
    assert ruff.group(1).split() == ["lint", "format-check"]
    assert "just mutation-clean" in justfile
    assert 'scopes+=(--scope "$filter")' in justfile
    assert 'mutation_runner.py run --max-children "{{ MUTATION_CHILDREN }}" $filters' in justfile
    assert 'MUTATION_CHILDREN := env("MUTATION_CHILDREN", "4")' in justfile


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
    assert "entry: just focused" in config
    assert "entry: just qa-push" in config
    assert "stages: [pre-push]" in config
    # The pre-push hook must stay bounded: a bare `pytest` here would run the
    # whole suite on every push, which is the pull request's job.
    assert "entry: uv run --locked pytest" not in config


def test_github_actions_is_read_only_pinned_and_runs_just() -> None:
    workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text()

    assert "contents: read" in workflow
    assert "uv sync --locked" in workflow
    assert "run: just qa-pr" in workflow
    # A superseded pull-request run must not keep a 25-job matrix alive.
    assert "concurrency:" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    # The container gate belongs to the Docker workflow; building it here too
    # would pay for the same image twice on every commit.
    assert "just smoke" not in workflow
    assert "mutation_filters:" in workflow
    assert "--json" in workflow
    assert "fromJSON(needs.quality.outputs.mutation_filters)" in workflow
    assert "fail-fast: false" in workflow
    assert "max-parallel: 24" in workflow
    assert 'run: just mutation-module "$MUTATION_FILTERS"' in workflow
    assert "fetch-depth: 0" in workflow
    assert "HF_TOKEN" not in workflow
    uses = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow)
    assert len(uses) == 6
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)


def test_production_text_io_declares_utf8_encoding() -> None:
    assert _implicit_text_io_calls() == []


def test_production_code_does_not_defer_first_party_imports() -> None:
    assert _nested_first_party_imports() == []


def test_a_full_sweep_starts_from_a_clean_workspace() -> None:
    """Verdicts persist between runs, so a stale workspace fakes the gate."""
    justfile = (ROOT / "justfile").read_text()

    assert "mutation: mutation-clean" in justfile
    assert "rm -rf mutants .pytest_cache" in justfile
    assert "-name '__pycache__' -type d -exec rm -rf {} +" in justfile
    # A scoped run keeps the workspace -- regenerating every mutant costs
    # minutes -- and tells the gate its scope instead.
    assert 'mutation-scope base="origin/main":\n' in justfile
    assert 'just mutation-gate "${scopes[@]}"' in justfile


def test_mutation_gate_runs_a_portable_baseline_check() -> None:
    """The gate ran ripgrep, which a bare CI image lacks, so it never fired."""
    justfile = (ROOT / "justfile").read_text()

    assert "rg -q" not in justfile
    assert "python scripts/quality/mutation_gate.py" in justfile
    assert "--baseline docs/quality/mutation-baseline.txt" in justfile
    assert (ROOT / "docs" / "quality" / "mutation-baseline.txt").is_file()


def test_the_mutation_sweep_workflow_is_manual_pinned_and_sharded() -> None:
    """A full sweep runs per area in parallel so no job outlives its runner."""
    workflow = (ROOT / ".github" / "workflows" / "mutation-sweep.yml").read_text()

    assert "workflow_dispatch:" in workflow
    # Tier 5: exhaustive and scheduled, never in a pull request's path.
    assert "schedule:" in workflow
    assert 'cron: "0 3 * * *"' in workflow
    assert "pull_request" not in workflow
    assert "contents: read" in workflow
    assert "HF_TOKEN" not in workflow
    assert "uv sync --locked" in workflow
    assert "mutation_runner.py run" in workflow
    assert "mutation_baseline.py" in workflow
    for area in ("pipeline", "reporting", "application", "publishing", "runtime"):
        assert f"          - {area}\n" in workflow
    uses = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow)
    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)


def test_the_quality_gates_are_tiered_and_do_not_duplicate_work() -> None:
    """Each tier must add something the cheaper tier below it did not prove.

    The costly mistakes this pins down are the ones the repository actually
    made: a pre-push hook that ran the whole suite, and a pull-request gate
    that ran the suite once plainly and once more under coverage.
    """
    justfile = (ROOT / "justfile").read_text()

    push = re.search(r'^qa-push base="origin/main":[ \t]*(.*)$', justfile, re.MULTILINE)
    assert push is not None
    assert push.group(1).strip() == "ruff typecheck"

    pr = re.search(r"^qa-pr:[ \t]*(.*)$", justfile, re.MULTILINE)
    assert pr is not None
    pr_gates = pr.group(1).split()
    assert pr_gates == ["baseline", "ruff", "typecheck", "coverage", "crap", "build"]
    # `coverage` already collects unit, acceptance and architecture, so naming
    # any of them again would run the suite twice.
    for duplicated in ("unit", "acceptance", "architecture", "test"):
        assert duplicated not in pr_gates

    merge = re.search(r"^qa-merge:[ \t]*(.*)$", justfile, re.MULTILINE)
    assert merge is not None
    assert merge.group(1).strip() == "qa-pr"

    # `crap` must not depend on `coverage`, or every caller pays for the suite
    # a second time; `qa-pr` is what sequences them.
    crap = re.search(r"^crap:[ \t]*(.*)$", justfile, re.MULTILINE)
    assert crap is not None
    assert crap.group(1).strip() == ""


def test_the_release_gate_stays_strict() -> None:
    """Data integrity is never selected away or sampled."""
    justfile = (ROOT / "justfile").read_text()

    release = re.search(r"^release-verify run_dir:\s*$", justfile, re.MULTILINE)
    assert release is not None
    assert 'osm-polygon-website-tag verify-results --run-dir "{{ run_dir }}"' in justfile
    assert 'just release-stats-dry-run "{{ run_dir }}"' in justfile
    # The publishing recipes must keep demanding an explicit repository.
    assert "--confirm-repo 'NoeFlandre/osm-polygon-website-tag'" in justfile


def test_the_suite_recipes_shard_across_cores() -> None:
    """The project lives on an external volume, so I/O latency sets the pace.

    Serially the suite spent about eighteen minutes mostly waiting on reads;
    sharded across cores it runs in well under two. Losing `-n auto` would
    quietly hand that back.
    """
    justfile = (ROOT / "justfile").read_text()

    for recipe in ("test", "unit", "acceptance", "coverage"):
        body = re.search(rf"^{recipe}:[^\n]*\n((?:    [^\n]*\n)+)", justfile, re.MULTILINE)
        assert body is not None, recipe
        assert "-n auto" in body.group(1), recipe
    assert "pytest-xdist" in (ROOT / "pyproject.toml").read_text()
