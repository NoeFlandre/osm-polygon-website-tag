"""Contracts for the bounded pre-push test selection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.quality import select_tests
from scripts.quality.select_tests import (
    ALWAYS,
    BROAD_SENTINEL,
    select,
)

_REAL_DIRS = {
    Path("tests/reporting"),
    Path("tests/reporting/geographic"),
    Path("tests/storage"),
    Path("tests/quality"),
    Path("tests/architecture"),
}


def _is_dir(path: Path) -> bool:
    return path in _REAL_DIRS


def _exists(path: Path) -> bool:
    return path in _REAL_DIRS or path.as_posix().endswith(".py")


def _select(paths: list[str]) -> list[str]:
    return select(paths, exists=_exists, is_dir=_is_dir)


def test_nothing_testable_selects_nothing() -> None:
    assert _select(["README.md", "docs/index.md"]) == []


def test_a_changed_module_selects_its_mirrored_test_package() -> None:
    assert _select(["src/osm_polygon_website_tag/storage/duckdb_engine.py"]) == [
        *sorted({"tests/storage", *ALWAYS})
    ]


def test_a_nested_module_selects_the_nested_test_package() -> None:
    """`geographic/basemap.py` is covered by `test_basemap_private.py`.

    Selecting the mirrored directory rather than a guessed `test_basemap.py`
    is what keeps that coverage in the selection.
    """
    selected = _select(["src/osm_polygon_website_tag/reporting/geographic/basemap.py"])

    assert "tests/reporting/geographic" in selected


def test_a_module_without_its_own_test_package_walks_up() -> None:
    selected = _select(["src/osm_polygon_website_tag/runtime/telemetry.py"])

    assert selected == []


def test_a_changed_test_file_selects_itself() -> None:
    selected = _select(["tests/reporting/test_card_stats.py"])

    assert "tests/reporting/test_card_stats.py" in selected


def test_a_changed_quality_script_selects_the_quality_tests() -> None:
    selected = _select(["scripts/quality/mutation_gate.py"])

    assert "tests/quality" in selected


def test_structural_tests_run_whenever_anything_testable_changed() -> None:
    selected = _select(["src/osm_polygon_website_tag/storage/duckdb_engine.py"])

    assert "tests/architecture" in selected


def test_configuration_changes_defeat_selection() -> None:
    """A dependency or fixture change can break anything, so say so."""
    for broad in ("pyproject.toml", "uv.lock", "justfile", "tests/conftest.py"):
        assert _select([broad]) == [BROAD_SENTINEL], broad


def test_a_broad_change_wins_over_a_narrow_one() -> None:
    selected = _select(
        ["src/osm_polygon_website_tag/storage/duckdb_engine.py", "tests/conftest.py"]
    )

    assert selected == [BROAD_SENTINEL]


def test_non_python_sources_are_ignored() -> None:
    assert _select(["src/osm_polygon_website_tag/reporting/geographic/README.md"]) == []


def test_selection_is_sorted_and_deduplicated() -> None:
    selected = _select(
        [
            "src/osm_polygon_website_tag/storage/duckdb_engine.py",
            "src/osm_polygon_website_tag/storage/bounded.py",
        ]
    )

    assert selected == sorted(set(selected))


def test_git_diff_filters_empty_names_and_preserves_diff_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        seen["command"] = command
        seen.update(kwargs)
        return SimpleNamespace(stdout="a.py\n\n b.py \n")

    monkeypatch.setattr(select_tests.subprocess, "run", fake_run)

    assert select_tests._git_diff("main...HEAD", cwd=tmp_path) == {"a.py", "b.py"}
    assert seen["command"] == ["git", "diff", "--name-only", "main...HEAD"]
    assert seen["cwd"] == tmp_path
    assert seen["check"] is True


def test_changed_paths_unions_branch_and_worktree_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_diff(*revisions: str, cwd: Path | None = None) -> set[str]:
        del cwd
        calls.append(" ".join(revisions))
        return {"branch.py"} if len(calls) == 1 else {"working.py", "branch.py"}

    monkeypatch.setattr(select_tests, "_git_diff", fake_diff)

    assert select_tests.changed_paths("main") == ["branch.py", "working.py"]
    assert calls == ["main...HEAD", "HEAD"]


def test_cli_prints_selected_paths(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(select_tests, "changed_paths", lambda base: [base])
    monkeypatch.setattr(select_tests, "select", lambda paths: paths)

    assert select_tests.main(["--base", "feature"]) == 0
    assert capsys.readouterr().out == "feature\n"
