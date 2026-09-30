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
kills is reported so the file can shrink; ``--strict-baseline`` makes that a
failure, so the baseline can only ever get smaller.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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
_NOT_CHECKED_LINE = re.compile(r"^\s*(?P<name>\S+):\s*not checked\s*$")


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
    return _mutants_matching(lines, _KILLED_LINE)


def reported_mutants(lines: Iterable[str]) -> set[str]:
    """Return every mutant with a verdict, including explicitly unchecked ones."""
    found: set[str] = set()
    for line in lines:
        for pattern in (_RESULT_LINE, _KILLED_LINE, _NOT_CHECKED_LINE):
            match = pattern.match(line)
            if match:
                found.add(match.group("name"))
                break
    return found


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


@dataclass(frozen=True)
class GateFindings:
    """Verdicts selected for one scope and compared with the recorded debt."""

    unverified: list[str]
    regressions: list[str]
    healed: list[str]
    reported: set[str]
    unchecked: set[str]


def _scoped_list(names: Iterable[str], scopes: Sequence[str]) -> list[str]:
    return [name for name in names if in_scope(name, scopes)]


def _scoped_set(names: Iterable[str], scopes: Sequence[str]) -> set[str]:
    return {name for name in names if in_scope(name, scopes)}


def _unchecked_mutants(lines: Iterable[str]) -> set[str]:
    return _mutants_matching(lines, _NOT_CHECKED_LINE)


def _mutants_matching(lines: Iterable[str], pattern: re.Pattern[str]) -> set[str]:
    """Collect names from lines matching one set-valued verdict pattern."""
    found: set[str] = set()
    for line in lines:
        match = pattern.match(line)
        if match:
            found.add(match.group("name"))
    return found


def _findings(lines: list[str], baseline: set[str], scopes: Sequence[str]) -> GateFindings:
    unverified = _scoped_list(unverified_mutants(lines), scopes)
    return GateFindings(
        unverified=unverified,
        regressions=[name for name in unverified if name not in baseline],
        healed=sorted(_scoped_set(baseline & killed_mutants(lines), scopes)),
        reported=_scoped_set(reported_mutants(lines), scopes),
        unchecked=_scoped_set(_unchecked_mutants(lines), scopes),
    )


def _report_missing_verdicts(scopes: Sequence[str], findings: GateFindings) -> bool:
    if findings.reported and not findings.reported <= findings.unchecked:
        return False
    scope = ", ".join(scopes) if scopes else "the full mutation run"
    print(
        f"Mutation gate failed: no mutation verdicts were generated for {scope}; "
        "the scope may have produced zero mutants or zero associated tests.",
        file=sys.stderr,
    )
    return True


def _report_healed(findings: GateFindings, baseline_path: Path, *, strict: bool) -> bool:
    if not findings.healed:
        return False
    stream = sys.stderr if strict else sys.stdout
    print(
        f"{len(findings.healed)} baseline mutant(s) are now killed; remove them from {baseline_path}:",
        file=stream,
    )
    for name in findings.healed:
        print(f"  {name}", file=stream)
    return strict


def _report_regressions(regressions: Sequence[str]) -> bool:
    if not regressions:
        return False
    print(
        f"Mutation gate failed: {len(regressions)} unverified mutant(s) outside the baseline.",
        file=sys.stderr,
    )
    for name in regressions:
        print(f"  {name}", file=sys.stderr)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Report the verdict of one mutation run against its baseline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument(
        "--strict-baseline",
        action="store_true",
        help="fail when a baseline entry is now killed, so the file must shrink with the code",
    )
    args = parser.parse_args(argv)
    lines = args.results.read_text(encoding="utf-8").splitlines()
    baseline = read_baseline(args.baseline)
    findings = _findings(lines, baseline, args.scope)
    if _report_missing_verdicts(args.scope, findings):
        return 1
    if _report_healed(findings, args.baseline, strict=args.strict_baseline):
        return 1
    if _report_regressions(findings.regressions):
        return 1
    print(
        "Mutation gate passed: every checked mutant was killed or is recorded "
        f"in the baseline ({len(findings.unverified)} baseline hit(s))."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
