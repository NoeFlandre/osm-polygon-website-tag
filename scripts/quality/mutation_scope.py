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


def module_filters(paths: Iterable[str]) -> list[str]:
    """Return one deterministic mutmut filter per changed package module."""
    filters: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if path.suffix != ".py" or PACKAGE_ROOT not in path.parents:
            continue
        relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
        parts = [part for part in relative.parts if part != "__init__"]
        filters.add(".".join([PACKAGE_NAME, *parts, "*"]))
    return sorted(filters)


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
