from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.quality import mutation_runner, mutation_scope

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / mutation_scope.PACKAGE_ROOT
_SWEEP = _ROOT / ".github" / "workflows" / "mutation-sweep.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_SWEEP.read_text(encoding="utf-8"))


def _steps(job: str) -> list[dict[str, Any]]:
    return _workflow()["jobs"][job]["steps"]


def _step(job: str, name: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r} in {job!r}"
    return matches[0]


def _shards() -> list[mutation_scope.NightlyShard]:
    return mutation_scope.full_sweep_shards(root=_ROOT)


def _area_source_modules(area: str) -> list[str]:
    package = _PACKAGE / area
    modules = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        parts = path.relative_to(_PACKAGE).with_suffix("").parts
        modules.append(".".join([mutation_scope.PACKAGE_NAME, *parts]))
    return modules


def test_sweep_never_runs_on_pull_requests() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"schedule", "workflow_dispatch"}


@pytest.mark.parametrize("shard", _shards(), ids=lambda shard: shard["id"])
def test_every_nightly_shard_maps_to_mutable_source(shard: mutation_scope.NightlyShard) -> None:
    filters = shard["filters"].split()
    if shard["pipeline"]:
        expected = mutation_scope.module_function_filters(shard["module"], root=_ROOT)
        assert expected is not None
        assert filters
        assert all(value in expected for value in filters)
        assert len(filters) <= mutation_scope.NIGHTLY_SHARD_FUNCTIONS
        return

    area = shard["id"]
    scope = mutation_runner._source_paths_for_mutant_names(filters)
    assert any(path.name != "__init__.py" for path in scope), area
    expected_filters = [
        mutant_filter
        for module in _area_source_modules(area)
        for mutant_filter in mutation_scope.module_function_filters(module, root=_ROOT) or []
    ]
    assert expected_filters


def test_full_sweep_plan_emits_a_dynamic_matrix() -> None:
    plan = _workflow()["jobs"]["plan"]
    output = plan["outputs"]["mutation_shards"]

    assert output == "${{ steps.resolve.outputs.matrix }}"
    resolve = next(step for step in plan["steps"] if step.get("id") == "resolve")
    assert "--full-sweep-json" in resolve["run"]

    matrix = _workflow()["jobs"]["area"]["strategy"]["matrix"]["shard"]
    assert matrix == "${{ fromJSON(needs.plan.outputs.mutation_shards) }}"


def test_every_pipeline_function_is_covered_by_one_matrix_shard() -> None:
    actual: dict[str, list[str]] = {}
    for shard in _shards():
        if shard["pipeline"]:
            actual.setdefault(shard["module"], []).extend(shard["filters"].split())

    expected = {
        "osm_polygon_website_tag." + ".".join(path.relative_to(_PACKAGE).with_suffix("").parts)
        for path in sorted((_PACKAGE / "pipeline").rglob("*.py"))
        if path.name != "__init__.py"
    }
    assert set(actual) == expected
    for module in expected:
        assert actual[module] == mutation_scope.module_function_filters(module, root=_ROOT)


def test_merge_requires_a_result_artifact_from_every_shard() -> None:
    merge = _step("merge", "Require a result artifact from every shard")
    passes = _step("merge", "Require every mutation shard to pass")

    assert merge["env"]["MUTATION_SHARDS_JSON"] == "${{ needs.plan.outputs.mutation_shards }}"
    assert passes["env"]["AREA_RESULT"] == "${{ needs.area.result }}"
    assert "results-{shard['id']}.txt" in merge["run"]
    assert (
        "mutation-results-${{ matrix.shard.id }}"
        in _step("area", "Upload the shard baseline")["with"]["name"]
    )


def test_mutation_jobs_are_bounded_and_do_not_cache_filtered_runs() -> None:
    area = _workflow()["jobs"]["area"]

    assert area["timeout-minutes"] == 120
    assert all(not step.get("uses", "").startswith("actions/cache/") for step in area["steps"])


def test_pipeline_checkpoints_are_frequent_and_matrix_stays_within_github_limit() -> None:
    shards = _shards()
    pipeline_shards = [shard for shard in shards if shard["pipeline"]]

    assert max(len(shard["filters"].split()) for shard in pipeline_shards) <= 2
    assert len(shards) <= 256


def test_pipeline_verification_is_scoped_to_one_module_and_filter_group() -> None:
    verify = _step("area", "Require all shard mutants to have verdicts")
    run = _step("area", "Sweep the shard")

    assert verify["if"] == "matrix.shard.pipeline == true"
    assert "--filters" in verify["run"]
    assert "MUTATION_FILTERS" in verify["env"]
    assert verify["env"]["MUTATION_MODULE"] == "${{ matrix.shard.module }}"
    assert "mutation_verdicts.py verify" in verify["run"]
    assert run["env"]["MUTATION_FILTERS"] == "${{ matrix.shard.filters }}"
    assert '"${filters[@]}"' in run["run"]


def test_shard_results_come_from_mutmut_without_masking_its_failure() -> None:
    run = _step("area", "Collect the shard's unverified mutants")["run"]

    assert "|| true" not in run
    assert "mutmut results --all true >" in run


def test_nightly_publishes_the_baseline_diff_and_still_fails_on_growth() -> None:
    merge = _step("merge", "Merge the shard results into a candidate baseline")["run"]
    upload = _step("merge", "Upload the baseline diff")

    assert "--fail-on-growth" in merge
    assert "--diff mutation-baseline.diff" in merge
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "mutation-baseline.diff"
    assert upload["uses"].startswith("actions/upload-artifact@")


def test_pull_request_gate_does_not_run_the_full_mutation_sweep() -> None:
    justfile = (_ROOT / "justfile").read_text(encoding="utf-8")
    recipe = re.search(r"^qa-pr:(?P<deps>.*)$", justfile, re.MULTILINE)

    assert recipe is not None
    assert "mutation" not in recipe.group("deps")
