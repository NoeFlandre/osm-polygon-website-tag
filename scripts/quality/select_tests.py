#!/usr/bin/env python3
"""Select the tests a change can plausibly break.

The pre-push gate must stay bounded: a full suite on every push trades minutes
of a person's attention for coverage the pull-request gate already provides on
every commit. This maps a diff to the smallest set of test paths that can still
fail for that diff, and says so explicitly when a change is broad enough that no
selection would be honest.

Test files do not always mirror a module one-to-one -- ``geographic/basemap.py``
is covered by ``test_basemap_private.py`` -- so a changed module selects its
mirrored test *directory* rather than a guessed file name. That is wider than
the minimum and deliberately so: a selection that silently misses its tests is
worse than one that runs a few extra.
"""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

PACKAGE_ROOT = Path("src/osm_polygon_website_tag")
TESTS_ROOT = Path("tests")

# A change to any of these invalidates selection itself: they can alter
# collection, fixtures, dependency versions or the gate definitions, so no
# per-module mapping is trustworthy. The caller decides what to run instead.
BROAD_PATHS = frozenset(
    {
        ".pre-commit-config.yaml",
        "justfile",
        "pyproject.toml",
        "tests/conftest.py",
        "uv.lock",
    }
)

# Structural checks are cheap and catch exactly the breakage a narrow selection
# is most likely to miss, so they run whenever anything testable changed.
ALWAYS = ("tests/architecture",)

BROAD_SENTINEL = "BROAD"

Predicate = Callable[[Path], bool]


def _git_diff(*revisions: str, cwd: Path | None = None) -> set[str]:
    """Return the paths one ``git diff`` invocation reports."""
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "--name-only", *revisions],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def changed_paths(base: str, *, cwd: Path | None = None) -> list[str]:
    """Return paths differing from ``base``, including uncommitted work.

    The hook runs before the commit exists, so the working tree has to count
    as much as the branch does.
    """
    return sorted(_git_diff(f"{base}...HEAD", cwd=cwd) | _git_diff("HEAD", cwd=cwd))


def _mirrored_test_dir(module: Path, is_dir: Predicate) -> Path | None:
    """Return the nearest existing test package mirroring one source module."""
    candidate = TESTS_ROOT / module.relative_to(PACKAGE_ROOT).parent
    while True:
        if is_dir(candidate):
            return candidate
        if candidate == TESTS_ROOT:
            return None
        candidate = candidate.parent


def select(
    paths: Sequence[str],
    *,
    exists: Predicate = Path.exists,
    is_dir: Predicate = Path.is_dir,
) -> list[str]:
    """Return pytest targets for ``paths``.

    Returns ``[BROAD_SENTINEL]`` when a change defeats selection, and an empty
    list when nothing testable changed.
    """
    if any(path in BROAD_PATHS for path in paths):
        return [BROAD_SENTINEL]
    targets: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if path.suffix != ".py":
            continue
        if raw.startswith(f"{TESTS_ROOT.as_posix()}/"):
            if exists(path):
                targets.add(raw)
        elif raw.startswith("scripts/"):
            targets.add("tests/quality")
        elif raw.startswith(f"{PACKAGE_ROOT.as_posix()}/"):
            mirrored = _mirrored_test_dir(path, is_dir)
            if mirrored is not None:
                targets.add(mirrored.as_posix())
    if not targets:
        return []
    return sorted({*targets, *ALWAYS})


def main(argv: Sequence[str] | None = None) -> int:
    """Print one pytest target per line for the current diff."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    for target in select(changed_paths(args.base)):
        print(target)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
