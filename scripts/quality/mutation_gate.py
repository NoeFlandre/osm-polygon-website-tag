#!/usr/bin/env python3
"""Fail a mutation run on any unverified mutant the baseline does not record.

The gate used to search the results with ripgrep, which the CI image does not
carry, so it never fired and a long-standing backlog of surviving mutants went
unnoticed. That backlog is recorded in a baseline file: every mutant listed
there is known debt, and anything else that survives fails the run.

Mutants outside the run's scope report ``not checked`` and are ignored, so a
scoped run judges only what it actually exercised. Verdicts also persist in the
mutant workspace between runs, so a scoped run passes its own scope with
``--scope`` and stale verdicts from other modules are left out rather than
wiping and regenerating the whole workspace. A baseline entry the suite now
kills is reported so the file can shrink.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

UNVERIFIED_VERDICTS = (
    "survived",
    "no tests",
    "timeout",
    "suspicious",
    "segfault",
    "check was interrupted",
)
_RESULT_LINE = re.compile(rf"^\s*(?P<name>\S+):\s*(?P<verdict>{'|'.join(UNVERIFIED_VERDICTS)})\s*$")
_KILLED_LINE = re.compile(r"^\s*(?P<name>\S+):\s*killed\s*$")


def unverified_mutants(lines: Iterable[str]) -> list[str]:
    """Return the mutants a run left unverified, in report order."""
    found = []
    for line in lines:
        match = _RESULT_LINE.match(line)
        if match:
            found.append(match.group("name"))
    return found


def killed_mutants(lines: Iterable[str]) -> set[str]:
    """Return the mutants a run killed."""
    return {match.group("name") for line in lines if (match := _KILLED_LINE.match(line))}


def in_scope(name: str, scopes: Sequence[str]) -> bool:
    """Return whether one mutant belongs to a run's scope.

    A scope is a mutmut filter such as ``package.module.*``; without any scope
    every reported mutant counts.
    """
    if not scopes:
        return True
    return any(name.startswith(scope.removesuffix("*")) for scope in scopes)


def read_baseline(path: Path) -> set[str]:
    """Read the recorded backlog, ignoring comments and blank lines."""
    if not path.is_file():
        return set()
    entries = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def main(argv: Sequence[str] | None = None) -> int:
    """Report the verdict of one mutation run against its baseline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--scope", action="append", default=[])
    args = parser.parse_args(argv)
    lines = args.results.read_text(encoding="utf-8").splitlines()
    baseline = read_baseline(args.baseline)
    unverified = [name for name in unverified_mutants(lines) if in_scope(name, args.scope)]
    regressions = [name for name in unverified if name not in baseline]
    healed = sorted(name for name in baseline & killed_mutants(lines) if in_scope(name, args.scope))
    if healed:
        print(f"{len(healed)} baseline mutant(s) are now killed; remove them from {args.baseline}:")
        for name in healed[:20]:
            print(f"  {name}")
    if regressions:
        print(
            f"Mutation gate failed: {len(regressions)} unverified mutant(s) outside the baseline.",
            file=sys.stderr,
        )
        for name in regressions[:40]:
            print(f"  {name}", file=sys.stderr)
        return 1
    print(
        "Mutation gate passed: every checked mutant was killed or is recorded "
        f"in the baseline ({len(unverified)} baseline hit(s))."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
