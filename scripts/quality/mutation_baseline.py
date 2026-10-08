#!/usr/bin/env python3
"""Write the mutation baseline from a completed sweep's results.

The baseline records the mutants the suite does not yet verify. It exists so
the gate can fail on a *new* survivor while a long-standing backlog stays
visible and reviewable, and it is only meaningful when generated from a full
sweep: a scoped run leaves most mutants unchecked.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.quality.mutation_gate import read_baseline, unverified_mutants

HEADER = (
    "# Mutation baseline: mutants the suite does not yet verify.",
    "#",
    "# Every name below is known debt, recorded so the gate can fail on a new",
    "# survivor instead of on this backlog. Shrink it: pick a module, write the",
    "# tests that kill its mutants, and delete the lines they cover.",
    "#",
    "# Regenerate from a full sweep:",
    "#   just mutation-clean",
    "#   uv run --locked python scripts/quality/mutation_runner.py run --max-children 2",
    "#   uv run --locked mutmut results --all true > results.txt",
    "#   uv run --locked python scripts/quality/mutation_baseline.py --results results.txt",
)


def render(names: Sequence[str]) -> str:
    """Return the baseline file content for one sweep's unverified mutants.

    The header count is derived from the same unique names as the entries, so
    it always equals the number of entries the gate reads back.
    """
    unique = sorted(set(names))
    return "\n".join([*HEADER, f"# Recorded mutants: {len(unique)}", "", *unique]) + "\n"


def unified_diff(current: str, candidate: str, *, current_name: str, candidate_name: str) -> str:
    """Return the reviewable diff from the recorded baseline to a regenerated one."""
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile=current_name,
            tofile=candidate_name,
        )
    )


def _write_baseline(target: Path, names: Sequence[str]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(names), encoding="utf-8")
    print(f"recorded {len(set(names))} unverified mutant(s) in {target}")


def _write_diff(diff: Path, baseline: Path, target: Path, candidate: str) -> None:
    """Write the baseline diff a reviewer reads when the sweep would change it."""
    current = baseline.read_text(encoding="utf-8") if baseline.is_file() else ""
    diff.parent.mkdir(parents=True, exist_ok=True)
    diff.write_text(
        unified_diff(
            current,
            candidate,
            current_name=str(baseline),
            candidate_name=str(target),
        ),
        encoding="utf-8",
    )


def _report_growth(
    *,
    fail_on_growth: bool,
    grown: Sequence[str],
    baseline: Path,
    output: Path | None,
    target: Path,
    names: Sequence[str],
) -> bool:
    if not fail_on_growth or not grown:
        return False
    _print_growth(grown, baseline)
    _write_candidate(output, baseline, target, names)
    return True


def _print_growth(grown: Sequence[str], baseline: Path) -> None:
    print(f"the sweep found {len(grown)} survivor(s) outside {baseline}:", file=sys.stderr)
    for name in grown:
        print(f"  {name}", file=sys.stderr)


def _write_candidate(
    output: Path | None, baseline: Path, target: Path, names: Sequence[str]
) -> None:
    if output is not None and output.resolve() != baseline.resolve():
        _write_baseline(target, names)


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite the baseline file from a results listing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=Path("docs/quality/mutation-baseline.txt"))
    parser.add_argument(
        "--output", type=Path, help="write the regenerated baseline here instead of --baseline"
    )
    parser.add_argument(
        "--fail-on-growth",
        action="store_true",
        help="exit 1 when the sweep left a survivor the current baseline does not record",
    )
    parser.add_argument(
        "--diff",
        type=Path,
        help="write a unified diff from --baseline to the regenerated baseline here",
    )
    args = parser.parse_args(argv)
    lines = args.results.read_text(encoding="utf-8").splitlines()
    names = unverified_mutants(lines)
    target = args.output or args.baseline
    grown = sorted(set(names) - read_baseline(args.baseline))
    if args.diff is not None:
        _write_diff(args.diff, args.baseline, target, render(names))
    if _report_growth(
        fail_on_growth=args.fail_on_growth,
        grown=grown,
        baseline=args.baseline,
        output=args.output,
        target=target,
        names=names,
    ):
        return 1
    _write_baseline(target, names)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
