"""Tests for the changed-module mutation scope used by CI."""

from __future__ import annotations

import json
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
        "src/osm_polygon_website_tag/application/cli/verify.py",
    ]

    assert mutation_scope.module_filters(paths, root=_ROOT) == [
        "osm_polygon_website_tag.application.cli.verify.*",
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
    matrix = json.loads(capsys.readouterr().out)

    # Sorted by module, and a function-scoped module passes through as one
    # shard under its plain name.
    assert matrix[0] == {
        "name": "osm_polygon_website_tag.publishing.release",
        "filters": "osm_polygon_website_tag.publishing.release.x_publish__mutmut_*",
    }
    # A whole-module scope is expanded into explicit per-function filters
    # rather than left as one opaque `.*` job. Bounded fan-out and the
    # positional naming are covered against a larger module below.
    card = matrix[1:]
    assert [f for entry in card for f in entry["filters"].split()] == (
        mutation_scope.module_function_filters("osm_polygon_website_tag.reporting.card", root=_ROOT)
    )
    assert all(f != "osm_polygon_website_tag.reporting.card.*" for f in card[0]["filters"].split())


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


def test_pure_additions_ignore_blank_and_comment_lines() -> None:
    diff = (
        "+++ b/src/pkg/mod.py\n"
        "@@ -5,0 +6,5 @@ def old() -> int:\n"
        "+\n+\n+# helper\n+def new() -> int:\n+    return 2\n"
    )

    assert mutation_scope.parse_diff(diff) == {"src/pkg/mod.py": {9, 10}}


def test_a_comment_replacing_code_still_counts() -> None:
    diff = "+++ b/src/pkg/mod.py\n@@ -3 +3 @@\n-LIMIT = 3\n+# LIMIT removed\n"

    assert mutation_scope.parse_diff(diff) == {"src/pkg/mod.py": {3}}


def test_parse_diff_tracks_line_numbers_across_hunks_and_files() -> None:
    diff = (
        "+++ b/a.py\n@@ -1,2 +1,2 @@\n-x\n-y\n+x = 1\n+\n"
        "@@ -9,0 +10,2 @@\n+\n+z = 2\n"
        "+++ b/b.py\n@@ -1 +1,0 @@\n-gone\n"
    )

    assert mutation_scope.parse_diff(diff) == {"a.py": {1, 2, 11}, "b.py": {0}}


def test_a_deletion_only_hunk_scopes_the_whole_module(tmp_path: Path) -> None:
    module = tmp_path / "src" / "osm_polygon_website_tag" / "reporting" / "shrunk.py"
    module.parent.mkdir(parents=True)
    module.write_text('"""Module."""\n\n\ndef kept() -> int:\n    return 1\n', encoding="utf-8")
    relative = "src/osm_polygon_website_tag/reporting/shrunk.py"
    lines = mutation_scope.parse_diff(f"+++ b/{relative}\n@@ -3 +2,0 @@\n-LIMIT = 3\n")

    assert mutation_scope.function_filters(lines, root=tmp_path) == {
        "osm_polygon_website_tag.reporting.shrunk": ["osm_polygon_website_tag.reporting.shrunk.*"]
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


def test_a_whole_module_shard_expands_to_every_function() -> None:
    """Sharding must cover exactly what the whole-module filter covered.

    Every mutmut mutant belongs to a function, so the per-function filters of
    a module are complete; if that ever stopped being true, sharding would
    silently narrow the gate.
    """
    module = "osm_polygon_website_tag.reporting.card_stats"
    expanded = mutation_scope.module_function_filters(module, root=_ROOT)
    matrix = mutation_scope.shards({module: [f"{module}.*"]}, root=_ROOT)

    assert expanded, "expected the module to expose functions"
    assert [f for entry in matrix for f in entry["filters"].split()] == expanded
    assert all(f.startswith(f"{module}.") and f.endswith("__mutmut_*") for f in expanded)


def test_shards_are_bounded_and_named_with_their_position() -> None:
    module = "osm_polygon_website_tag.reporting.card_stats"
    matrix = mutation_scope.shards({module: [f"{module}.*"]}, root=_ROOT, size=8)

    assert len(matrix) > 1
    assert all(len(entry["filters"].split()) <= 8 for entry in matrix)
    assert matrix[0]["name"] == f"{module} [1/{len(matrix)}]"
    assert matrix[-1]["name"] == f"{module} [{len(matrix)}/{len(matrix)}]"


def test_a_single_shard_keeps_the_plain_module_name() -> None:
    module = "osm_polygon_website_tag.reporting.card_stats"
    matrix = mutation_scope.shards(
        {module: [f"{module}.x_compute_card_stats__mutmut_*"]}, root=_ROOT
    )

    assert matrix == [{"name": module, "filters": f"{module}.x_compute_card_stats__mutmut_*"}]


def test_a_module_without_functions_gets_no_shard(tmp_path: Path) -> None:
    """mutmut only mutates functions, so such a shard has nothing to run.

    It used to keep a whole-module filter, and mutmut then stopped with "could
    not find any test case for any mutant", failing CI on a constants module.
    """
    module = f"{mutation_scope.PACKAGE_NAME}.constants"
    source = tmp_path / mutation_scope.PACKAGE_ROOT / "constants.py"
    source.parent.mkdir(parents=True)
    source.write_text("class Settings:\n    value: int = 1\n", encoding="utf-8")
    other = f"{mutation_scope.PACKAGE_NAME}.other"
    (source.parent / "other.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    assert mutation_scope.module_function_filters(module, root=tmp_path) == []
    assert mutation_scope.shards(
        {module: [f"{module}.*"], other: [f"{other}.*"]}, root=tmp_path
    ) == [{"name": other, "filters": f"{other}.x_f__mutmut_*"}]


def test_an_unreadable_module_keeps_its_whole_module_filter(tmp_path: Path) -> None:
    module = f"{mutation_scope.PACKAGE_NAME}.missing"

    assert mutation_scope.module_function_filters(module, root=tmp_path) is None
    assert mutation_scope.shards({module: [f"{module}.*"]}, root=tmp_path) == [
        {"name": module, "filters": f"{module}.*"}
    ]


def test_methods_are_sharded_under_their_mutmut_prefix(tmp_path: Path) -> None:
    module = f"{mutation_scope.PACKAGE_NAME}.holder"
    source = tmp_path / mutation_scope.PACKAGE_ROOT / "holder.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Store:\n    def append(self) -> None:\n        pass\n", encoding="utf-8"
    )

    assert mutation_scope.module_function_filters(module, root=tmp_path) == [
        f"{module}.xǁStoreǁappend__mutmut_*"
    ]


def test_a_hunk_of_only_blank_or_hash_lines_scopes_the_whole_module() -> None:
    diff = "+++ b/src/pkg/mod.py\n@@ -7,0 +8,2 @@\n+\n+# inside a triple-quoted string\n"

    assert mutation_scope.parse_diff(diff) == {"src/pkg/mod.py": {0}}


def test_the_package_root_resolves_to_its_init_and_gets_no_shard_without_functions() -> None:
    root = mutation_scope.PACKAGE_NAME

    assert mutation_scope.module_function_filters(root, root=_ROOT) == []
    assert mutation_scope.shards({root: [f"{root}.*"]}, root=_ROOT) == []


_DECORATED = """import functools

import typer

app = typer.Typer()


@app.command("go")
def go_command() -> int:
    return 1


@functools.cache
def cached(x: int) -> int:
    def inner() -> int:
        return x
    return inner()


@functools.lru_cache
@functools.wraps(cached)
def stacked() -> int:
    return 2


def plain() -> int:
    return 3


@dataclass
class Box:
    def method(self) -> int:
        return 4


class Tools:
    @staticmethod
    def static() -> int:
        return 5

    @classmethod
    def build(cls) -> int:
        return 6

    @property
    def prop(self) -> int:
        return 7

    def normal(self) -> int:
        return 8
"""


def _write_decorated(tmp_path: Path) -> str:
    source = tmp_path / mutation_scope.PACKAGE_ROOT / "decorated.py"
    source.parent.mkdir(parents=True)
    source.write_text(_DECORATED, encoding="utf-8")
    return f"{mutation_scope.PACKAGE_NAME}.decorated"


def test_functions_mutmut_never_mutates_get_no_filter(tmp_path: Path) -> None:
    module = _write_decorated(tmp_path)

    filters = mutation_scope.module_function_filters(module, root=tmp_path)

    prefix = f"{module}."
    assert filters == [
        f"{prefix}x_plain__mutmut_*",
        f"{prefix}xǁToolsǁstatic__mutmut_*",
        f"{prefix}xǁToolsǁbuild__mutmut_*",
        f"{prefix}xǁToolsǁnormal__mutmut_*",
    ]


def test_a_change_inside_a_decorated_function_does_not_scope_the_module(tmp_path: Path) -> None:
    module = _write_decorated(tmp_path)
    path = f"{mutation_scope.PACKAGE_ROOT}/decorated.py"
    lines = _DECORATED.splitlines()
    inside_command = lines.index("    return 1") + 1
    on_decorator = lines.index('@app.command("go")') + 1
    inside_class = lines.index("    def method(self) -> int:") + 1

    scoped = mutation_scope.function_filters(
        {path: {inside_command, on_decorator, inside_class}}, root=tmp_path
    )

    assert module not in scoped


def test_a_change_beside_a_decorated_function_still_scopes_only_that_function(
    tmp_path: Path,
) -> None:
    module = _write_decorated(tmp_path)
    path = f"{mutation_scope.PACKAGE_ROOT}/decorated.py"
    plain_body = _DECORATED.splitlines().index("    return 3") + 1

    scoped = mutation_scope.function_filters({path: {plain_body}}, root=tmp_path)

    assert scoped == {module: [f"{module}.x_plain__mutmut_*"]}


def test_a_shard_of_only_decorated_functions_is_not_emitted(tmp_path: Path) -> None:
    source = tmp_path / mutation_scope.PACKAGE_ROOT / "commands.py"
    source.parent.mkdir(parents=True)
    source.write_text('@app.command("a")\ndef a() -> int:\n    return 1\n', encoding="utf-8")
    module = f"{mutation_scope.PACKAGE_NAME}.commands"

    assert mutation_scope.shards({module: [f"{module}.*"]}, root=tmp_path) == []
