#!/usr/bin/env python3
"""Print the mutmut filters covering the package modules a change touches.

A full sweep of this package is roughly fourteen thousand mutants and takes
hours, which a hosted CI runner does not reliably survive. Scoping the gate to
the modules a change actually touches keeps it enforcing on new code while the
full sweep stays a deliberate local or scheduled run.

Each printed line is one ``mutmut run`` filter, for example
``osm_polygon_website_tag.pipeline.sat.*``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

PACKAGE_ROOT = Path("src/osm_polygon_website_tag")
PACKAGE_NAME = "osm_polygon_website_tag"
TESTS_ROOT = Path("tests")
_TEST_PREFIX = "test_"


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
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    return [part for part in relative.parts if part != "__init__"]


def _mirrored_source_parts(path: Path, root: Path) -> list[str] | None:
    """Return the module parts a changed test file mirrors, when one exists."""
    if TESTS_ROOT not in path.parents or not path.name.startswith(_TEST_PREFIX):
        return None
    relative = path.relative_to(TESTS_ROOT).with_suffix("")
    parts = [*relative.parts[:-1], relative.parts[-1].removeprefix(_TEST_PREFIX)]
    if not (root / PACKAGE_ROOT / Path(*parts).with_suffix(".py")).is_file():
        return None
    return parts


def main(argv: Sequence[str] | None = None) -> int:
    """Print one filter per line, or nothing when no module changed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    try:
        paths = changed_paths(args.base)
    except subprocess.CalledProcessError as exc:
        print(f"error: cannot diff against {args.base!r}: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    for filter_expression in module_filters(paths):
        print(filter_expression)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
