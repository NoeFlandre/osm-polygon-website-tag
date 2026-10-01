#!/usr/bin/env python3
"""Report CRAP scores for selected Python modules.

The report joins Radon's cyclomatic complexity with function-level coverage
from a Coverage JSON report.  A function, or a whole file, missing from the
coverage report is an error rather than 0% coverage: a silent zero would either
hide a broken join or blame code the tests do run.  The command is
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
        if "functions" not in file_data:
            continue
        entries = _function_coverage_entries(file_data["functions"])
        raw = Path(raw_path)
        candidates = {_path_key(raw), _path_key(Path.cwd() / raw)}
        for candidate in candidates:
            indexed[candidate] = entries
    return indexed


def _function_coverage_entries(functions: dict[str, Any]) -> dict[int, float]:
    """Index usable function summaries from one Coverage JSON file record."""
    entries: dict[int, float] = {}
    for function_data in functions.values():
        start_line = function_data.get("start_line")
        summary = function_data.get("summary", {})
        covered = summary.get("percent_covered")
        if isinstance(start_line, int) and isinstance(covered, (int, float)):
            entries[start_line] = float(covered)
    return entries


def _blocks(blocks: Iterable[Any]) -> Iterable[Any]:
    """Yield each function, method and nested closure once, excluding class aggregates."""
    seen: set[tuple[str, int]] = set()
    for block in _walk(blocks):
        key = (str(block.name), int(block.lineno))
        if key in seen:
            continue
        seen.add(key)
        yield block


def _walk(blocks: Iterable[Any]) -> Iterable[Any]:
    """Depth-first over ``cc_visit`` blocks: a class yields its methods, a function its closures.

    Radon 6 lists methods both at the top level and under their class, and
    closures only under their function; deduplication by name and line makes
    either layout score each callable once.
    """
    for block in blocks:
        if block.__class__.__name__ == "Class":
            yield from _walk(getattr(block, "methods", ()))
            continue
        yield block
        yield from _walk(getattr(block, "closures", ()))


def _path_candidates(paths: Sequence[Path]) -> Iterable[Path]:
    """Yield source files from each input in stable directory order."""
    for path in paths:
        candidates = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        yield from candidates


def _expand_paths(paths: Sequence[Path]) -> list[Path]:
    """Expand directory inputs into deterministic, existing Python paths."""
    expanded: list[Path] = []
    seen: set[Path] = set()
    for candidate in _path_candidates(paths):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            expanded.append(candidate)
    return expanded


class CoverageJoinError(ValueError):
    """Raised when a scanned file or function has no coverage entry to join."""


def _file_coverage(path: Path, coverage_by_file: dict[str, dict[int, float]]) -> dict[int, float]:
    """Return the per-line function coverage for ``path``, or fail if it is absent."""
    by_line = coverage_by_file.get(_path_key(path))
    if by_line is None:
        raise CoverageJoinError(
            f"{path} is missing from the coverage report (or the report has no per-function "
            "data; coverage 7.5 or newer is required)"
        )
    return by_line


def _score(path: Path, block: Any, by_line: dict[int, float]) -> FunctionScore:
    line = int(block.lineno)
    if line not in by_line:
        raise CoverageJoinError(f"{path}:{line} {block.name} has no coverage entry at that line")
    return FunctionScore(
        path=path,
        name=str(block.name),
        line=line,
        complexity=int(block.complexity),
        coverage_percent=by_line[line],
    )


def score_paths(paths: Sequence[Path], coverage: dict[str, Any]) -> list[FunctionScore]:
    """Return deterministic function scores for ``paths``."""
    coverage_by_file = _coverage_functions(coverage)
    scores: list[FunctionScore] = []
    for path in _expand_paths(paths):
        by_line = _file_coverage(path, coverage_by_file)
        blocks = _blocks(cc_visit(path.read_text(encoding="utf-8")))
        scores.extend(_score(path, block, by_line) for block in blocks)
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
    lines.extend(
        f"{score.path}:{score.line}  {score.name}  {score.complexity:11d}"
        f"  {score.coverage_percent:7.1f}%  {score.crap:5.2f}"
        for score in scores
    )
    return "\n".join(lines)


def _report_scores(scores: Sequence[FunctionScore], max_crap: float) -> int:
    """Print scores and enforce the exclusive CRAP threshold."""
    if not scores:
        print("unable to build CRAP report: no functions found", file=sys.stderr)
        return 2
    print(_render(scores))
    failures = [score for score in scores if score.crap >= max_crap]
    if failures:
        print(
            f"{len(failures)} function(s) are at or above the CRAP threshold {max_crap:.2f}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report and return a process exit code."""
    args = _parser().parse_args(argv)
    try:
        coverage = json.loads(args.coverage_json.read_text(encoding="utf-8"))
        scores = score_paths(args.path, coverage)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:  # CoverageJoinError too
        print(f"unable to build CRAP report: {exc}", file=sys.stderr)
        return 2
    return _report_scores(scores, args.max_crap)


if __name__ == "__main__":
    raise SystemExit(main())
