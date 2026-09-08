"""Tests for the changed-module mutation scope used by CI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.quality import mutation_scope


def test_only_package_modules_become_filters() -> None:
    paths = [
        "src/osm_polygon_website_tag/pipeline/sat.py",
        "src/osm_polygon_website_tag/reporting/verification/sentence.py",
        "src/osm_polygon_website_tag/__init__.py",
        "tests/pipeline/test_sat.py",
        "docs/operations.md",
        "scripts/quality/mutation_scope.py",
        "src/osm_polygon_website_tag/pipeline/README.md",
    ]

    assert mutation_scope.module_filters(paths) == [
        "osm_polygon_website_tag.*",
        "osm_polygon_website_tag.pipeline.sat.*",
        "osm_polygon_website_tag.reporting.verification.sentence.*",
    ]


def test_filters_are_deduplicated_and_sorted() -> None:
    paths = [
        "src/osm_polygon_website_tag/pipeline/sat.py",
        "src/osm_polygon_website_tag/pipeline/sat.py",
        "src/osm_polygon_website_tag/application/cli.py",
    ]

    assert mutation_scope.module_filters(paths) == [
        "osm_polygon_website_tag.application.cli.*",
        "osm_polygon_website_tag.pipeline.sat.*",
    ]


def test_no_package_change_produces_no_filters() -> None:
    assert mutation_scope.module_filters(["docs/operations.md", "justfile"]) == []


def test_changed_paths_excludes_deletions(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="a.py\n\n b.py \n", stderr="")

    monkeypatch.setattr(mutation_scope.subprocess, "run", fake_run)

    assert mutation_scope.changed_paths("origin/main") == ["a.py", "b.py"]
    assert recorded == [
        ["git", "diff", "--name-only", "--diff-filter=d", "origin/main...HEAD"],
    ]


def test_main_prints_one_filter_per_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        mutation_scope,
        "changed_paths",
        lambda base: ["src/osm_polygon_website_tag/pipeline/sat.py"] if base == "main" else [],
    )

    assert mutation_scope.main(["--base", "main"]) == 0
    assert capsys.readouterr().out == "osm_polygon_website_tag.pipeline.sat.*\n"

    assert mutation_scope.main(["--base", "other"]) == 0
    assert capsys.readouterr().out == ""


def test_main_defaults_to_the_upstream_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(mutation_scope, "changed_paths", lambda base: seen.append(base) or [])

    assert mutation_scope.main([]) == 0
    assert seen == ["origin/main"]


def test_main_reports_an_unusable_base(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(base: str) -> list[str]:
        raise subprocess.CalledProcessError(128, ["git"], stderr="bad revision\n")

    monkeypatch.setattr(mutation_scope, "changed_paths", explode)

    assert mutation_scope.main(["--base", "nope"]) == 2
    assert "cannot diff against 'nope'" in capsys.readouterr().err


def test_real_repository_diff_is_readable() -> None:
    """The scope must work against a real ref, not only a stubbed git."""
    paths = mutation_scope.changed_paths("HEAD", cwd=Path(__file__).resolve().parents[2])

    assert paths == []
