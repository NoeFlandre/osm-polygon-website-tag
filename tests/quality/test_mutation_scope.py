"""Tests for the changed-module mutation scope used by CI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.quality import mutation_scope

_ROOT = Path(__file__).resolve().parents[2]


def test_only_package_modules_become_filters() -> None:
    paths = [
        "src/osm_polygon_website_tag/pipeline/sat.py",
        "src/osm_polygon_website_tag/reporting/verification/sentence.py",
        "src/osm_polygon_website_tag/__init__.py",
        "docs/operations.md",
        "scripts/quality/mutation_runner.py",
        "src/osm_polygon_website_tag/pipeline/README.md",
    ]

    assert mutation_scope.module_filters(paths, root=_ROOT) == [
        "osm_polygon_website_tag.*",
        "osm_polygon_website_tag.pipeline.sat.*",
        "osm_polygon_website_tag.reporting.verification.sentence.*",
    ]


def test_package_initializers_map_to_their_package_module() -> None:
    assert mutation_scope.module_filters(
        ["src/osm_polygon_website_tag/reporting/geographic/__init__.py"], root=_ROOT
    ) == ["osm_polygon_website_tag.reporting.geographic.*"]
    assert mutation_scope.module_filters(
        ["src/osm_polygon_website_tag/__init__.py"], root=_ROOT
    ) == ["osm_polygon_website_tag.*"]
    assert mutation_scope.module_filters(
        ["src/osm_polygon_website_tag/reporting/geographic/aggregation.py"], root=_ROOT
    ) == ["osm_polygon_website_tag.reporting.geographic.aggregation.*"]


def test_filters_are_deduplicated_and_sorted() -> None:
    paths = [
        "src/osm_polygon_website_tag/pipeline/sat.py",
        "src/osm_polygon_website_tag/pipeline/sat.py",
        "src/osm_polygon_website_tag/application/cli.py",
    ]

    assert mutation_scope.module_filters(paths, root=_ROOT) == [
        "osm_polygon_website_tag.application.cli.*",
        "osm_polygon_website_tag.pipeline.sat.*",
    ]


def test_no_package_change_produces_no_filters() -> None:
    assert mutation_scope.module_filters(["docs/operations.md", "justfile"], root=_ROOT) == []


def test_module_filters_recheck_the_module_a_changed_test_mirrors() -> None:
    """Weakening a test must not slip past the gate untested.

    This covers `module_filters`, which `main` does not call; the CI path is
    covered by `test_the_ci_scope_rechecks_the_module_a_changed_test_mirrors`.
    Asserting the contract only here is what let the gap go unnoticed.
    """
    paths = [
        "tests/pipeline/test_sat.py",
        "tests/reporting/geographic/test_h3_geometry.py",
    ]

    assert mutation_scope.module_filters(paths, root=_ROOT) == [
        "osm_polygon_website_tag.pipeline.sat.*",
        "osm_polygon_website_tag.reporting.geographic.h3_geometry.*",
    ]


def test_tests_without_a_mirrored_module_are_ignored() -> None:
    paths = [
        "tests/quality/test_mutation_scope.py",
        "tests/architecture/test_tooling.py",
        "tests/conftest.py",
        "tests/fixtures/polygon_shards.py",
        "tests/pipeline/test_nothing_like_this.py",
    ]

    assert mutation_scope.module_filters(paths, root=_ROOT) == []


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
        "changed_lines",
        lambda base: {"src/osm_polygon_website_tag/pipeline/sat.py": {1}} if base == "main" else {},
    )
    monkeypatch.setattr(
        mutation_scope,
        "function_filters",
        lambda lines: (
            {"osm_polygon_website_tag.pipeline.sat": ["osm_polygon_website_tag.pipeline.sat.*"]}
            if lines
            else {}
        ),
    )

    assert mutation_scope.main(["--base", "main"]) == 0
    assert capsys.readouterr().out == "osm_polygon_website_tag.pipeline.sat.*\n"

    assert mutation_scope.main(["--base", "other"]) == 0
    assert capsys.readouterr().out == ""


def test_main_can_emit_a_sorted_json_matrix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mutation_scope, "changed_lines", lambda base: {"x": {1}})
    monkeypatch.setattr(
        mutation_scope,
        "function_filters",
        lambda lines: {
            "osm_polygon_website_tag.reporting.card": ["osm_polygon_website_tag.reporting.card.*"],
            "osm_polygon_website_tag.publishing.release": [
                "osm_polygon_website_tag.publishing.release.x_publish__mutmut_*"
            ],
        },
    )

    assert mutation_scope.main(["--base", "main", "--json"]) == 0
    assert capsys.readouterr().out == (
        '[{"name":"osm_polygon_website_tag.publishing.release",'
        '"filters":"osm_polygon_website_tag.publishing.release.x_publish__mutmut_*"},'
        '{"name":"osm_polygon_website_tag.reporting.card",'
        '"filters":"osm_polygon_website_tag.reporting.card.*"}]\n'
    )


def test_main_defaults_to_the_upstream_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(mutation_scope, "changed_lines", lambda base: seen.append(base) or {})

    assert mutation_scope.main([]) == 0
    assert seen == ["origin/main"]


def test_main_reports_an_unusable_base(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(base: str) -> dict[str, set[int]]:
        raise subprocess.CalledProcessError(128, ["git"], stderr="bad revision\n")

    monkeypatch.setattr(mutation_scope, "changed_lines", explode)

    assert mutation_scope.main(["--base", "nope"]) == 2
    assert "cannot diff against 'nope'" in capsys.readouterr().err


def test_real_repository_diff_is_readable() -> None:
    """The scope must work against a real ref, not only a stubbed git."""
    paths = mutation_scope.changed_paths("HEAD", cwd=Path(__file__).resolve().parents[2])

    assert paths == []


_SOURCE = '''\
"""Module."""


def kept() -> int:
    """Untouched."""
    return 1


def changed() -> int:
    """Touched."""
    return 2


class Holder:
    """Holder."""

    def method(self) -> int:
        """Touched method."""
        return 3
'''


def test_changed_functions_scope_to_their_own_mutants(tmp_path: Path) -> None:
    module = tmp_path / "src" / "osm_polygon_website_tag" / "reporting" / "sample.py"
    module.parent.mkdir(parents=True)
    module.write_text(_SOURCE, encoding="utf-8")
    relative = "src/osm_polygon_website_tag/reporting/sample.py"

    filters = mutation_scope.function_filters({relative: {11, 19}}, root=tmp_path)

    assert filters == {
        "osm_polygon_website_tag.reporting.sample": [
            "osm_polygon_website_tag.reporting.sample.x_changed__mutmut_*",
            "osm_polygon_website_tag.reporting.sample.xǁHolderǁmethod__mutmut_*",
        ]
    }


def test_a_change_outside_every_function_scopes_the_whole_module(tmp_path: Path) -> None:
    module = tmp_path / "src" / "osm_polygon_website_tag" / "reporting" / "sample.py"
    module.parent.mkdir(parents=True)
    module.write_text(_SOURCE, encoding="utf-8")
    relative = "src/osm_polygon_website_tag/reporting/sample.py"

    filters = mutation_scope.function_filters({relative: {1}}, root=tmp_path)

    assert filters == {
        "osm_polygon_website_tag.reporting.sample": ["osm_polygon_website_tag.reporting.sample.*"]
    }


def test_a_changed_import_does_not_charge_the_whole_module(tmp_path: Path) -> None:
    module = tmp_path / "src" / "osm_polygon_website_tag" / "reporting" / "imports.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        '"""Module."""\n\nfrom pathlib import Path\n\n\ndef only(value: Path) -> Path:\n'
        '    """Only."""\n    return value\n',
        encoding="utf-8",
    )
    relative = "src/osm_polygon_website_tag/reporting/imports.py"

    assert mutation_scope.function_filters({relative: {3}}, root=tmp_path) == {}
    assert mutation_scope.function_filters({relative: {3, 8}}, root=tmp_path) == {
        "osm_polygon_website_tag.reporting.imports": [
            "osm_polygon_website_tag.reporting.imports.x_only__mutmut_*"
        ]
    }


def test_a_changed_module_constant_still_charges_the_whole_module(tmp_path: Path) -> None:
    module = tmp_path / "src" / "osm_polygon_website_tag" / "reporting" / "constant.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        '"""Module."""\n\nLIMIT = 3\n\n\ndef only() -> int:\n    """Only."""\n    return LIMIT\n',
        encoding="utf-8",
    )
    relative = "src/osm_polygon_website_tag/reporting/constant.py"

    assert mutation_scope.function_filters({relative: {3}}, root=tmp_path) == {
        "osm_polygon_website_tag.reporting.constant": [
            "osm_polygon_website_tag.reporting.constant.*"
        ]
    }


def test_the_json_matrix_carries_a_name_and_its_filters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        mutation_scope,
        "changed_lines",
        lambda base: {"src/osm_polygon_website_tag/reporting/card.py": {1}},
    )
    monkeypatch.setattr(
        mutation_scope,
        "function_filters",
        lambda lines: {"osm_polygon_website_tag.reporting.card": ["a.x_one__mutmut_*", "a.x_b*"]},
    )

    assert mutation_scope.main(["--json"]) == 0

    assert capsys.readouterr().out.strip() == (
        '[{"name":"osm_polygon_website_tag.reporting.card","filters":"a.x_one__mutmut_* a.x_b*"}]'
    )


def test_the_ci_scope_rechecks_the_module_a_changed_test_mirrors() -> None:
    """The CI path must honour the mirroring, not just `module_filters`.

    `main` scopes with `function_filters`; for a while only `module_filters`
    understood test files, so a test-only change selected nothing and a
    weakened test could retire its module's mutants unverified.
    """
    scoped = mutation_scope.function_filters(
        {"tests/reporting/test_card_stats.py": {1}}, root=_ROOT
    )

    assert scoped == {
        "osm_polygon_website_tag.reporting.card_stats": [
            "osm_polygon_website_tag.reporting.card_stats.*"
        ]
    }


def test_a_test_file_without_a_mirrored_module_selects_nothing() -> None:
    assert (
        mutation_scope.function_filters({"tests/quality/test_crap_report.py": {1}}, root=_ROOT)
        == {}
    )
    assert mutation_scope.function_filters({"tests/conftest.py": {1}}, root=_ROOT) == {}


def test_a_changed_test_widens_its_module_beyond_the_changed_functions() -> None:
    """A test edit can reach any mutant, so it outranks a function filter."""
    scoped = mutation_scope.function_filters(
        {
            "tests/reporting/test_card_stats.py": {1},
            "src/osm_polygon_website_tag/reporting/card_stats.py": {1},
        },
        root=_ROOT,
    )

    assert scoped["osm_polygon_website_tag.reporting.card_stats"] == [
        "osm_polygon_website_tag.reporting.card_stats.*"
    ]
