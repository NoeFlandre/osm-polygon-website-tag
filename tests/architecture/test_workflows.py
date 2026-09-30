"""Behavioural checks on the GitHub Actions workflows, read as data."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _jobs() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path.name, job_id, job)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job_id, job in _load(path.name)["jobs"].items()
    ]


@pytest.mark.parametrize(("workflow", "job_id", "job"), _jobs())
def test_every_job_has_a_timeout(workflow: str, job_id: str, job: dict[str, Any]) -> None:
    timeout = job.get("timeout-minutes")

    assert isinstance(timeout, int), f"{workflow}:{job_id} runs without a timeout"
    assert 0 < timeout <= 360


def test_the_aggregate_check_waits_for_every_other_quality_job() -> None:
    jobs = _load("quality.yml")["jobs"]

    assert set(jobs["ci-ok"]["needs"]) == set(jobs) - {"ci-ok"}


def test_the_aggregate_check_runs_even_when_a_needed_job_fails() -> None:
    assert _load("quality.yml")["jobs"]["ci-ok"]["if"] == "always()"


def test_the_aggregate_check_accepts_only_success_and_skipped() -> None:
    script = _load("quality.yml")["jobs"]["ci-ok"]["steps"][0]["run"]

    assert "success|skipped) ;;" in script
    assert "exit 1" in script


def test_advisory_benchmarks_do_not_block_the_aggregate_check() -> None:
    job = _load("quality.yml")["jobs"]["ci-ok"]
    results = job["steps"][0]["env"]["RESULTS"]

    assert "benchmarks" in job["needs"]
    assert "needs.benchmarks.result" not in results
    for required_job in ("scope", "quality", "docs", "docker", "mutation"):
        assert f"needs.{required_job}.result" in results


def test_performance_job_saves_a_base_measurement_and_compares_medians() -> None:
    job = _load("quality.yml")["jobs"]["benchmarks"]
    base = next(step for step in job["steps"] if step.get("name") == "Time the base")
    compare = next(step for step in job["steps"] if step.get("name") == "Time the head against it")
    justfile = (WORKFLOWS.parent.parent / "justfile").read_text(encoding="utf-8")

    assert "--benchmark-save=base" in base["run"]
    assert "--benchmark-json=" in base["run"]
    assert "just bench-compare" in compare["run"]
    assert "OSM_POLY_BENCHMARK_ACCEPTANCE=1" in justfile
    assert "--benchmark-compare-fail=median:25%" in justfile
    assert "--benchmark-json=" in justfile


def test_the_docs_build_gates_pull_requests() -> None:
    workflow = _load("quality.yml")
    steps = workflow["jobs"]["docs"]["steps"]
    triggers = {key: value for key, value in workflow.items() if key in ("on", True)}

    assert "pull_request" in next(iter(triggers.values()))  # PyYAML reads bare `on` as True
    assert any("mkdocs build --strict" in step.get("run", "") for step in steps)


def test_mutation_shards_start_beside_the_quality_gate_and_skip_pushes() -> None:
    mutation = _load("quality.yml")["jobs"]["mutation"]

    assert mutation["needs"] == "scope"
    assert "github.event_name == 'pull_request'" in mutation["if"]


def test_nightly_mutation_merge_requires_every_area_artifact() -> None:
    jobs = _load("mutation-sweep.yml")["jobs"]
    areas = jobs["area"]["strategy"]["matrix"]["area"]
    merge_steps = jobs["merge"]["steps"]
    require_step = next(
        step
        for step in merge_steps
        if step.get("name") == "Require a result artifact from every area"
    )
    setup_uv = next(step for step in jobs["area"]["steps"] if "setup-uv" in step.get("uses", ""))

    assert 'artifact="areas/results-${area}.txt"' in require_step["run"]
    assert '[[ ! -f "$artifact" ]]' in require_step["run"]
    assert all(area in require_step["run"] for area in areas)
    assert setup_uv["with"]["cache-dependency-glob"] == "uv.lock"


def test_only_the_pages_deploy_job_may_write_pages() -> None:
    jobs = _load("docs.yml")["jobs"]

    assert "pages" not in jobs["build"].get("permissions", {})
    assert jobs["deploy"]["permissions"]["pages"] == "write"
