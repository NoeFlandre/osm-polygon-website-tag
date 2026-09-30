#!/usr/bin/env python3
"""Print the mutmut filters covering the code a change touches.

A full sweep of this package is roughly fourteen thousand mutants and takes
hours, which a hosted CI runner does not reliably survive. Scoping the gate to
what a change actually touches keeps it enforcing on new code while the full
sweep stays a deliberate local or scheduled run.

The scope is per function, not per module. Mutating a whole module whenever a
change touches one of its lines charges that change for every mutant its file
already carried, so a one-line edit to a delegation module owed coverage for
code it never read. A changed line outside every function -- an import, a
module constant, a decorator -- still scopes its whole module, because module
level code can change any behaviour in the file; a changed import is the one
exception, since it cannot alter a function the change never touched.

Each emitted filter is one ``mutmut run`` filter, for example
``osm_polygon_website_tag.pipeline.sat.x_segment__mutmut_*``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

PACKAGE_ROOT = Path("src/osm_polygon_website_tag")
PACKAGE_NAME = "osm_polygon_website_tag"
TESTS_ROOT = Path("tests")
_TEST_PREFIX = "test_"
_HUNK = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def changed_paths(base: str, *, cwd: Path | None = None) -> list[str]:
    """Return the repository paths a change adds or modifies against ``base``.

    Deletions are excluded: a removed module has no mutants left to check.
    """
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD"],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def module_filters(paths: Iterable[str], *, root: Path | None = None) -> list[str]:
    """Return one deterministic mutmut filter per module a change reaches.

    A changed test file re-checks the module it mirrors: weakening a test would
    otherwise leave that module's mutants unverified until some later change
    happened to touch it.
    """
    base = root if root is not None else Path.cwd()
    filters: set[str] = set()
    for raw in paths:
        module_filter = _module_filter(Path(raw), base)
        if module_filter is not None:
            filters.add(module_filter)
    return sorted(filters)


def _module_filter(path: Path, root: Path) -> str | None:
    """Map a changed source or mirrored test path to its whole-module filter."""
    if path.suffix != ".py":
        return None
    parts = _source_parts(path)
    if parts is None:
        parts = _mirrored_source_parts(path, root)
    if parts is None:
        return None
    return ".".join([PACKAGE_NAME, *parts, "*"])


def _source_parts(path: Path) -> list[str] | None:
    """Return the module parts of a changed package source file."""
    if PACKAGE_ROOT not in path.parents:
        return None
    relative = path.relative_to(PACKAGE_ROOT)
    if relative.name == "__init__.py":
        return list(relative.parent.parts)
    return list(relative.with_suffix("").parts)


def _mirrored_source_parts(path: Path, root: Path) -> list[str] | None:
    """Return the module parts a changed test file mirrors, when one exists."""
    if TESTS_ROOT not in path.parents or not path.name.startswith(_TEST_PREFIX):
        return None
    relative = path.relative_to(TESTS_ROOT).with_suffix("")
    parts = [*relative.parts[:-1], relative.parts[-1].removeprefix(_TEST_PREFIX)]
    if not (root / PACKAGE_ROOT / Path(*parts).with_suffix(".py")).is_file():
        return None
    return parts


def changed_lines(base: str, *, cwd: Path | None = None) -> dict[str, set[int]]:
    """Return the added or modified line numbers per changed repository path."""
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "-U0", "--diff-filter=d", f"{base}...HEAD"],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_diff(result.stdout)


def parse_diff(diff: str) -> dict[str, set[int]]:
    """Return the added or modified new-side line numbers of a ``-U0`` diff.

    Line 0 lies outside every function, so recording it scopes the whole
    module. That is recorded for a deletion-only hunk, and for a hunk that
    keeps no line. In a pure-addition hunk (nothing removed), blank and
    comment-only lines are dropped: adding a function also adds blank
    separators, which must not charge the whole module. Such a hunk that
    holds only those lines may be text inside a string, so it scopes the
    module. In any other hunk every new line counts, since a comment or
    blank line there may replace module-level code.
    """
    lines: dict[str, set[int]] = {}
    for path, pure_addition, start, added in _hunks(diff):
        lines.setdefault(path, set()).update(
            _kept_hunk_lines(start, added, pure_addition=pure_addition)
        )
    return lines


def _kept_hunk_lines(start: int, added: list[str], *, pure_addition: bool) -> list[int]:
    """Keep behavior-changing added lines, or mark a module-level hunk with zero."""
    kept = [
        start + offset
        for offset, text in enumerate(added)
        if not (pure_addition and _is_blank_or_comment(text))
    ]
    return kept or [0]


def _emit_hunk(
    hunk: tuple[str, bool, int, list[str]] | None,
) -> tuple[tuple[str, bool, int, list[str]], ...]:
    """Make the optional pending hunk yieldable without branching the parser."""
    return (hunk,) if hunk is not None else ()


def _append_added_line(hunk: tuple[str, bool, int, list[str]] | None, raw: str) -> None:
    """Record one added diff line when a hunk is open."""
    if hunk is not None and raw.startswith("+"):
        hunk[3].append(raw[1:])


def _hunks(diff: str) -> Iterator[tuple[str, bool, int, list[str]]]:
    """Yield ``(path, pure addition, first new line, added lines)`` per hunk."""
    hunk: tuple[str, bool, int, list[str]] | None = None
    current: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            yield from _emit_hunk(hunk)
            current = raw[len("+++ b/") :].strip()
            hunk = None
        if current is None:
            continue
        match = _HUNK.match(raw)
        if match is not None:
            yield from _emit_hunk(hunk)
            hunk = (current, match.group("old") == "0", int(match.group("start")), [])
            continue
        _append_added_line(hunk, raw)
    yield from _emit_hunk(hunk)


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def function_filters(
    lines_by_path: dict[str, set[int]], *, root: Path | None = None
) -> dict[str, list[str]]:
    """Return the mutmut filters per module for the functions a change touches.

    A module whose change falls outside every function keeps its whole-module
    filter, because module level code can alter anything the file defines.
    """
    base = root if root is not None else Path.cwd()
    scoped: dict[str, list[str]] = {}
    whole_module: set[str] = set()
    for raw, lines in sorted(lines_by_path.items()):
        path = Path(raw)
        if path.suffix != ".py":
            continue
        scope = _path_scope(path, lines, base)
        if scope is not None:
            _merge_scope(scope, scoped, whole_module)
    return scoped


def _path_scope(path: Path, lines: set[int], root: Path) -> tuple[str, list[str], bool] | None:
    """Choose the source-function or mirrored-test policy for one Python path."""
    parts = _source_parts(path)
    if parts is None:
        return _test_scope(path, root)
    return _source_scope(path, parts, lines, root)


def _test_scope(path: Path, root: Path) -> tuple[str, list[str], bool] | None:
    """Scope a changed test to the whole source module whose contract it covers."""
    mirrored = _mirrored_source_parts(path, root)
    if mirrored is None:
        return None
    module = ".".join([PACKAGE_NAME, *mirrored])
    return module, [f"{module}.*"], True


def _source_scope(
    path: Path, parts: list[str], lines: set[int], root: Path
) -> tuple[str, list[str], bool] | None:
    """Map changed source lines to their enclosing functions or whole module."""
    if not (root / path).is_file():
        return None
    module = ".".join([PACKAGE_NAME, *parts])
    names = _changed_function_names(root / path, lines)
    if names is None:
        return module, [f"{module}.*"], True
    if not names:
        return None
    filters = [f"{module}.{name}__mutmut_*" for name in sorted(names)]
    return module, filters, False


def _merge_scope(
    scope: tuple[str, list[str], bool],
    scoped: dict[str, list[str]],
    whole_module: set[str],
) -> None:
    module, filters, is_whole_module = scope
    if is_whole_module:
        whole_module.add(module)
        scoped[module] = filters
    elif module not in whole_module:
        scoped[module] = filters


def _changed_function_names(source: Path, lines: set[int]) -> set[str] | None:
    """Return the mutant prefixes a change reaches, or ``None`` for whole-module."""
    tree = _read_source_tree(source)
    if tree is None:
        return None
    covered, names = _changed_functions(tree, lines)
    return names if lines <= covered else None


def _read_source_tree(source: Path) -> ast.Module | None:
    """Parse one source module, using whole-module scope if it cannot be read."""
    try:
        return ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None


def _changed_functions(tree: ast.Module, lines: set[int]) -> tuple[set[int], set[str]]:
    """Collect covered source spans and the mutable functions touched by a diff."""
    covered: set[int] = set(_import_lines(tree))
    names: set[str] = set()
    for name, start, end in _iter_functions(tree):
        span = range(start, end + 1)
        covered.update(span)
        if name is not None and any(line in span for line in lines):
            names.add(name)
    return covered, names


def _import_lines(tree: ast.Module) -> Iterator[int]:
    """Yield the lines of every module level import.

    An import cannot change what an untouched function does, so adding a name
    to an import block should not charge a change for the whole file.
    """
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield from range(node.lineno, (node.end_lineno or node.lineno) + 1)


def _iter_functions(
    tree: ast.AST, class_name: str | None = None
) -> Iterator[tuple[str | None, int, int]]:
    """Yield the mutant prefix and line span of every function definition.

    A function mutmut never mutates (see ``_is_unmutated``) is yielded with a
    ``None`` prefix so its lines still count as covered by a function; nothing
    inside it or its class is yielded separately.
    """
    for node in ast.iter_child_nodes(tree):
        yield from _function_blocks(node, class_name)


def _function_blocks(
    node: ast.AST, class_name: str | None
) -> Iterator[tuple[str | None, int, int]]:
    """Yield mutation spans for one function or class and its nested callables."""
    if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return
    start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    end = node.end_lineno or node.lineno
    if _is_unmutated(node):
        yield None, start, end
        return
    yield from _mutable_function_blocks(node, class_name, start, end)


def _mutable_function_blocks(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    class_name: str | None,
    start: int,
    end: int,
) -> Iterator[tuple[str | None, int, int]]:
    """Yield a mutable class's methods or one function and its nested closures."""
    if isinstance(node, ast.ClassDef):
        yield from _iter_functions(node, node.name)
        return
    prefix = f"x\u01c1{class_name}\u01c1{node.name}" if class_name else f"x_{node.name}"
    yield prefix, start, end
    yield from _iter_functions(node, class_name)


def _is_unmutated(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether mutmut skips ``node``: it mutates neither decorated classes nor
    decorated functions, except a lone ``staticmethod`` or ``classmethod``.

    Typer commands and dataclass methods are therefore never mutants, and a
    shard listing only them would make mutmut stop with "nothing matches".
    """
    decorators = node.decorator_list
    if isinstance(node, ast.ClassDef):
        return bool(decorators)
    lone = decorators[0] if len(decorators) == 1 else None
    transparent = isinstance(lone, ast.Name) and lone.id in _TRANSPARENT_DECORATORS
    return bool(decorators) and not transparent


_TRANSPARENT_DECORATORS = frozenset({"staticmethod", "classmethod"})


# One shard per this many functions. A module is the unit of *selection* but a
# poor unit of *work*: `reporting/card_stats.py` alone carries 819 mutants and
# ran for 88 minutes while every other shard had long finished, so the whole
# matrix was as slow as its worst module. Splitting a module's functions across
# shards costs a little setup per job and buys back most of that wall clock.
SHARD_FUNCTIONS = 8


def module_function_filters(module: str, *, root: Path | None = None) -> list[str] | None:
    """Return one filter per function in ``module``, or ``None`` if unreadable.

    Every mutmut mutant belongs to a function -- module level code is not
    mutated -- so the per-function filters of a module cover exactly what its
    whole-module filter covers.
    """
    base = root if root is not None else Path.cwd()
    tree = _read_source_tree(_module_source(module, base))
    if tree is None:
        return None
    return [f"{module}.{name}__mutmut_*" for name, _start, _end in _iter_functions(tree) if name]


def _module_source(module: str, root: Path) -> Path:
    """Resolve a package module name to its Python source path."""
    relative = [] if module == PACKAGE_NAME else module.removeprefix(f"{PACKAGE_NAME}.").split(".")
    candidate = root / PACKAGE_ROOT.joinpath(*relative)
    return candidate / "__init__.py" if candidate.is_dir() else candidate.with_suffix(".py")


def shards(
    scoped: dict[str, list[str]],
    *,
    root: Path | None = None,
    size: int = SHARD_FUNCTIONS,
) -> list[dict[str, str]]:
    """Return the CI matrix: one entry per bounded group of function filters."""
    matrix: list[dict[str, str]] = []
    for module in sorted(scoped):
        matrix.extend(_module_shards(module, scoped[module], root=root, size=size))
    return matrix


def _expanded_filters(module: str, filters: list[str], root: Path | None) -> list[str] | None:
    """Expand a whole-module filter, or omit a module known to have no mutants."""
    if filters != [f"{module}.*"]:
        return filters
    expanded = module_function_filters(module, root=root)
    if expanded == []:
        return None
    return expanded or filters


def _module_shards(
    module: str,
    original_filters: list[str],
    *,
    root: Path | None,
    size: int,
) -> list[dict[str, str]]:
    filters = _expanded_filters(module, original_filters, root)
    if filters is None:
        return []
    groups = [filters[start : start + size] for start in range(0, len(filters), size)] or [filters]
    return [
        {
            "name": _shard_name(module, index, len(groups)),
            "filters": " ".join(group),
        }
        for index, group in enumerate(groups, start=1)
    ]


def _shard_name(module: str, index: int, total: int) -> str:
    """Keep one-shard names compact and number every split module shard."""
    return module if total == 1 else f"{module} [{index}/{total}]"


def _print_filters(scoped: dict[str, list[str]], *, json_output: bool) -> None:
    """Render either the shard matrix or the flat command-line filter list."""
    if json_output:
        print(json.dumps(shards(scoped), separators=(",", ":")))
        return
    for module in sorted(scoped):
        for filter_expression in scoped[module]:
            print(filter_expression)


def main(argv: Sequence[str] | None = None) -> int:
    """Print one filter per line, or the CI matrix of one shard per module."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON object per module, each with its own filters, for a CI matrix",
    )
    args = parser.parse_args(argv)
    try:
        lines = changed_lines(args.base)
    except subprocess.CalledProcessError as exc:
        print(f"error: cannot diff against {args.base!r}: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    scoped = function_filters(lines)
    _print_filters(scoped, json_output=args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
