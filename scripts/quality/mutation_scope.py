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
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


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
        path = Path(raw)
        if path.suffix != ".py":
            continue
        parts = _source_parts(path)
        if parts is None:
            parts = _mirrored_source_parts(path, base)
        if parts is not None:
            filters.add(".".join([PACKAGE_NAME, *parts, "*"]))
    return sorted(filters)


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
    lines: dict[str, set[int]] = {}
    current: str | None = None
    for raw in result.stdout.splitlines():
        if raw.startswith("+++ b/"):
            current = raw[len("+++ b/") :].strip()
        elif current is not None and (match := _HUNK.match(raw)):
            start = int(match.group("start"))
            count = int(match.group("count") or 1)
            lines.setdefault(current, set()).update(range(start, start + count))
    return lines


def function_filters(
    lines_by_path: dict[str, set[int]], *, root: Path | None = None
) -> dict[str, list[str]]:
    """Return the mutmut filters per module for the functions a change touches.

    A module whose change falls outside every function keeps its whole-module
    filter, because module level code can alter anything the file defines.
    """
    base = root if root is not None else Path.cwd()
    scoped: dict[str, list[str]] = {}
    for raw, lines in sorted(lines_by_path.items()):
        path = Path(raw)
        parts = _source_parts(path) if path.suffix == ".py" else None
        if parts is None:
            continue
        module = ".".join([PACKAGE_NAME, *parts])
        source = base / path
        if not source.is_file():
            continue
        names = _changed_function_names(source, lines)
        if names is None:
            scoped[module] = [f"{module}.*"]
        elif names:
            scoped[module] = [f"{module}.{name}__mutmut_*" for name in sorted(names)]
    return scoped


def _changed_function_names(source: Path, lines: set[int]) -> set[str] | None:
    """Return the mutant prefixes a change reaches, or ``None`` for whole-module."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None
    covered: set[int] = set(_import_lines(tree))
    names: set[str] = set()
    for name, start, end in _iter_functions(tree):
        span = range(start, end + 1)
        covered.update(span)
        if any(line in span for line in lines):
            names.add(name)
    if any(line not in covered for line in lines):
        return None
    return names


def _import_lines(tree: ast.Module) -> Iterator[int]:
    """Yield the lines of every module level import.

    An import cannot change what an untouched function does, so adding a name
    to an import block should not charge a change for the whole file.
    """
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield from range(node.lineno, (node.end_lineno or node.lineno) + 1)


def _iter_functions(tree: ast.AST, class_name: str | None = None) -> Iterator[tuple[str, int, int]]:
    """Yield the mutant prefix and line span of every function definition."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield from _iter_functions(node, node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            prefix = f"x\u01c1{class_name}\u01c1{node.name}" if class_name else f"x_{node.name}"
            yield prefix, node.lineno, node.end_lineno or node.lineno
            yield from _iter_functions(node, class_name)


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
    if args.json:
        matrix = [
            {"name": module, "filters": " ".join(filters)}
            for module, filters in sorted(scoped.items())
        ]
        print(json.dumps(matrix, separators=(",", ":")))
    else:
        for filters in (scoped[module] for module in sorted(scoped)):
            for filter_expression in filters:
                print(filter_expression)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
