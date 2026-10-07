"""Release gate decisions over fixture GitHub API responses (#145)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.quality import release_gate

SHA = "a" * 40
OTHER_SHA = "b" * 40
QUALITY = ".github/workflows/quality.yml"


def _run(
    run_id: int,
    *,
    event: str = "push",
    branch: str = "main",
    path: str = QUALITY,
    sha: str = SHA,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "path": path,
        "event": event,
        "head_branch": branch,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "check_suite_id": run_id + 1000,
    }


def _check(
    check_id: int,
    *,
    suite_id: int,
    name: str = "ci-ok",
    slug: str = "github-actions",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "check_suite": {"id": suite_id},
        "app": {"slug": slug},
    }


def _decide(runs: list[dict[str, Any]], checks: list[dict[str, Any]], sha: str = SHA) -> str:
    return release_gate.decide(sha, runs, checks)


def test_the_intended_push_run_with_a_passing_gate_passes() -> None:
    runs = [_run(1)]
    checks = [_check(10, suite_id=1001)]

    assert _decide(runs, checks) == release_gate.PASSED


@pytest.mark.parametrize(
    "run",
    [
        _run(1, event="pull_request"),
        _run(1, event="workflow_dispatch"),
        _run(1, branch="feature"),
        _run(1, path=".github/workflows/release.yml"),
        _run(1, sha=OTHER_SHA),
    ],
    ids=["pull_request", "manual", "other-branch", "other-workflow", "other-sha"],
)
def test_a_passing_gate_from_a_non_qualifying_run_is_not_accepted(run: dict[str, Any]) -> None:
    checks = [_check(10, suite_id=run["check_suite_id"])]

    assert _decide([run], checks) != release_gate.PASSED


def test_an_unrelated_passing_ci_ok_does_not_satisfy_an_absent_main_run() -> None:
    checks = [_check(10, suite_id=5555)]

    assert _decide([], checks) == release_gate.PENDING


def test_an_unrelated_passing_ci_ok_is_ignored_while_the_main_run_is_pending() -> None:
    runs = [_run(1, status="in_progress", conclusion=None)]
    checks = [_check(10, suite_id=5555), _check(11, suite_id=1001, status="in_progress")]

    assert _decide(runs, checks) == release_gate.PENDING


def test_a_pending_main_run_without_its_gate_yet_is_pending() -> None:
    runs = [_run(1, status="in_progress", conclusion=None)]

    assert _decide(runs, [_check(10, suite_id=5555)]) == release_gate.PENDING


def test_a_failed_gate_on_the_main_run_fails() -> None:
    runs = [_run(1, conclusion="failure")]
    checks = [_check(10, suite_id=1001, conclusion="failure")]

    assert _decide(runs, checks) == release_gate.FAILED


def test_a_cancelled_gate_on_the_main_run_fails() -> None:
    runs = [_run(1, conclusion="cancelled")]
    checks = [_check(10, suite_id=1001, conclusion="cancelled")]

    assert _decide(runs, checks) == release_gate.FAILED


def test_a_completed_main_run_without_a_gate_check_fails_closed() -> None:
    assert _decide([_run(1)], []) == release_gate.FAILED


def test_a_ci_ok_check_from_another_app_does_not_count() -> None:
    checks = [_check(10, suite_id=1001, slug="someone-else")]

    assert _decide([_run(1)], checks) == release_gate.FAILED


def test_a_check_named_differently_does_not_count() -> None:
    checks = [_check(10, suite_id=1001, name="quality")]

    assert _decide([_run(1)], checks) == release_gate.FAILED


def test_the_newest_qualifying_run_decides() -> None:
    runs = [_run(1, conclusion="failure"), _run(2)]
    checks = [_check(10, suite_id=1001, conclusion="failure"), _check(11, suite_id=1002)]

    assert _decide(runs, checks) == release_gate.PASSED


def test_the_newest_check_for_the_gate_decides_a_rerun() -> None:
    runs = [_run(1)]
    checks = [_check(10, suite_id=1001, conclusion="failure"), _check(12, suite_id=1001)]

    assert _decide(runs, checks) == release_gate.PASSED


def test_main_reads_the_api_fixtures_and_passes(tmp_path: Path) -> None:
    runs_path = tmp_path / "runs.json"
    checks_path = tmp_path / "checks.json"
    runs_path.write_text(json.dumps([_run(1)]), encoding="utf-8")
    checks_path.write_text(json.dumps([_check(10, suite_id=1001)]), encoding="utf-8")

    code = release_gate.main(
        ["--sha", SHA, "--runs", str(runs_path), "--check-runs", str(checks_path)]
    )

    assert code == 0


def test_the_release_verify_job_uses_the_gate_and_reads_actions() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "release.yml").read_text("utf-8"))
    verify = workflow["jobs"]["verify"]
    script = "\n".join(step.get("run", "") for step in verify["steps"])

    assert "scripts/quality/release_gate.py" in script
    assert "actions/workflows/quality.yml/runs" in script
    assert verify["permissions"]["actions"] == "read"
    assert verify["permissions"]["contents"] == "read"
