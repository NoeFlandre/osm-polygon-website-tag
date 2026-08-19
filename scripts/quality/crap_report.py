#!/usr/bin/env python3
"""Report CRAP scores for selected Python modules.

The report joins Radon's cyclomatic complexity with function-level coverage
from a Coverage JSON report.  A function absent from the coverage report is
treated as uncovered, which keeps the gate conservative.  The command is
deliberately read-only: it parses existing files and never runs application
code or touches generated data.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from radon.complexity import cc_visit

DEFAULT_MAX_CRAP = 6.0


@dataclass(frozen=True)
class FunctionScore:
    """Complexity, coverage, and CRAP score for one function."""

    path: Path
    name: str
    line: int
    complexity: int
    coverage_percent: float

    @property
    def crap(self) -> float:
        """Return the CRAP score defined by complexity and test coverage."""
        uncovered = 1.0 - self.coverage_percent / 100.0
        return self.complexity**2 * uncovered**3 + self.complexity


def _path_key(path: Path) -> str:
    """Return a normalized absolute path for matching coverage entries."""
    return str(path.resolve())


def _coverage_functions(coverage: dict[str, Any]) -> dict[str, dict[int, float]]:
    """Index coverage percentages by normalized file path and start line."""
    indexed: dict[str, dict[int, float]] = {}
    for raw_path, file_data in coverage.get("files", {}).items():
        functions = file_data.get("functions", {})
        entries: dict[int, float] = {}
        for function_data in functions.values():
            start_line = function_data.get("start_line")
            summary = function_data.get("summary", {})
            if isinstance(start_line, int) and isinstance(
                summary.get("percent_covered"), (int, float)
            ):
                entries[start_line] = float(summary["percent_covered"])
        raw = Path(raw_path)
        candidates = {_path_key(raw), _path_key(Path.cwd() / raw)}
        for candidate in candidates:
            indexed[candidate] = entries
    return indexed


def _blocks(blocks: Iterable[Any]) -> Iterable[Any]:
    """Yield Radon blocks, including methods nested in classes."""
    for block in blocks:
        yield block
        yield from getattr(block, "methods", ())


def score_paths(paths: Sequence[Path], coverage: dict[str, Any]) -> list[FunctionScore]:
    """Return deterministic function scores for ``paths``."""
    coverage_by_file = _coverage_functions(coverage)
    scores: list[FunctionScore] = []
    for path in paths:
        resolved = path.resolve()
        by_line = coverage_by_file.get(_path_key(resolved), {})
        for block in _blocks(cc_visit(path.read_text(encoding="utf-8"))):
            if not hasattr(block, "complexity") or not hasattr(block, "lineno"):
                continue
            scores.append(
                FunctionScore(
                    path=path,
                    name=str(block.name),
                    line=int(block.lineno),
                    complexity=int(block.complexity),
                    coverage_percent=by_line.get(int(block.lineno), 0.0),
                )
            )
    return sorted(scores, key=lambda score: (-score.crap, str(score.path), score.line, score.name))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-json", type=Path, required=True)
    parser.add_argument("--path", type=Path, action="append", required=True)
    parser.add_argument("--max-crap", type=float, default=DEFAULT_MAX_CRAP)
    return parser


def _render(scores: Sequence[FunctionScore]) -> str:
    lines = ["Path:line  Function  Complexity  Coverage  CRAP"]
    lines.append("-" * 58)
    for score in scores:
        lines.append(
            f"{score.path}:{score.line}  {score.name}  {score.complexity:11d}"
            f"  {score.coverage_percent:7.1f}%  {score.crap:5.2f}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report and return a process exit code."""
    args = _parser().parse_args(argv)
    try:
        coverage = json.loads(args.coverage_json.read_text(encoding="utf-8"))
        scores = score_paths(args.path, coverage)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"unable to build CRAP report: {exc}", file=sys.stderr)
        return 2
    if not scores:
        print("unable to build CRAP report: no functions found", file=sys.stderr)
        return 2

    print(_render(scores))
    failures = [score for score in scores if score.crap >= args.max_crap]
    if failures:
        print(
            f"{len(failures)} function(s) are at or above the CRAP threshold {args.max_crap:.2f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
