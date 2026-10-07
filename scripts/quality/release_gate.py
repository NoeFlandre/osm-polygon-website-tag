#!/usr/bin/env python3
"""Decide whether a tagged commit has a passing Quality gate on main (#145).

A release must not publish on a successful ``ci-ok`` alone: that check name is
shared by every Quality run, including pull request and manual ones. The gate
accepts only the ``ci-ok`` job of a push run of ``quality.yml`` on ``main`` for
the exact commit. The rest of that run's conclusion is advisory and is ignored,
so an advisory benchmark failure never blocks a release.

The release job feeds this module the workflow runs and the check runs of the
commit as JSON. Exit status: 0 passed, 2 still pending (wait and ask again),
1 failed, or absent once the run has completed.

Standard library only, so the release job runs it with the runner's ``python3``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

QUALITY_WORKFLOW_PATH = ".github/workflows/quality.yml"
GATE_CHECK_NAME = "ci-ok"
GATE_APP_SLUG = "github-actions"

PASSED = "passed"
PENDING = "pending"
FAILED = "failed"

EXIT_CODES = {PASSED: 0, PENDING: 2, FAILED: 1}

Record = Mapping[str, Any]


def qualifying_run(sha: str, runs: Sequence[Record]) -> Record | None:
    """Return the newest Quality push run on main for ``sha``, if any."""
    matches = [
        run
        for run in runs
        if run.get("path") == QUALITY_WORKFLOW_PATH
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("head_sha") == sha
    ]
    if not matches:
        return None
    return max(matches, key=lambda run: int(run["id"]))


def gate_check(run: Record, check_runs: Sequence[Record]) -> Record | None:
    """Return the newest ``ci-ok`` check that belongs to the run's check suite."""
    matches = [
        check
        for check in check_runs
        if check.get("name") == GATE_CHECK_NAME
        and (check.get("check_suite") or {}).get("id") == run.get("check_suite_id")
        and (check.get("app") or {}).get("slug") == GATE_APP_SLUG
    ]
    if not matches:
        return None
    return max(matches, key=lambda check: int(check["id"]))


def decide(sha: str, runs: Sequence[Record], check_runs: Sequence[Record]) -> str:
    """Return ``PASSED``, ``PENDING`` or ``FAILED`` for the tagged commit."""
    run = qualifying_run(sha, runs)
    if run is None:
        return PENDING
    gate = gate_check(run, check_runs)
    if gate is None:
        return FAILED if run.get("status") == "completed" else PENDING
    if gate.get("status") != "completed":
        return PENDING
    return PASSED if gate.get("conclusion") == "success" else FAILED


def _load(path: Path) -> list[Record]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide the release's Quality gate.")
    parser.add_argument("--sha", required=True, help="the tagged commit")
    parser.add_argument("--runs", type=Path, required=True, help="JSON array of workflow runs")
    parser.add_argument("--check-runs", type=Path, required=True, help="JSON array of check runs")
    args = parser.parse_args(argv)

    outcome = decide(args.sha, _load(args.runs), _load(args.check_runs))
    print(f"Quality gate for {args.sha}: {outcome}")
    return EXIT_CODES[outcome]


if __name__ == "__main__":
    sys.exit(main())
