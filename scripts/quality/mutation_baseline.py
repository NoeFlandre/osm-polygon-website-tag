#!/usr/bin/env python3
"""Write the mutation baseline from a completed sweep's results.

The baseline records the mutants the suite does not yet verify. It exists so
the gate can fail on a *new* survivor while a long-standing backlog stays
visible and reviewable, and it is only meaningful when generated from a full
sweep: a scoped run leaves most mutants unchecked.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from scripts.quality.mutation_gate import unverified_mutants

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
    """Return the baseline file content for one sweep's unverified mutants."""
    unique = sorted(set(names))
    return "\n".join([*HEADER, f"# Recorded mutants: {len(unique)}", "", *unique]) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite the baseline file from a results listing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=Path("docs/quality/mutation-baseline.txt"))
    args = parser.parse_args(argv)
    lines = args.results.read_text(encoding="utf-8").splitlines()
    names = unverified_mutants(lines)
    args.baseline.parent.mkdir(parents=True, exist_ok=True)
    args.baseline.write_text(render(names), encoding="utf-8")
    print(f"recorded {len(set(names))} unverified mutant(s) in {args.baseline}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
