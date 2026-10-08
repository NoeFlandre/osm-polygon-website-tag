"""Contract tests for the nightly mutation sweep workflow and its area mapping.

The nightly sweep runs for hours and cannot run in a pull request, so these
tests check the workflow file itself: which areas it sweeps, how a missing or
failed area is reported, and that the baseline diff is published. Each test
reads repository files only; none runs mutmut.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.quality import mutation_runner, mutation_scope

_ROOT = Path(__file__).resolve().parents[2]
_SWEEP = _ROOT / ".github" / "workflows" / "mutation-sweep.yml"
_PACKAGE = _ROOT / "src" / "osm_polygon_website_tag"
_AREA_LIST = re.compile(r"^\s*areas=\(([^)]*)\)", re.MULTILINE)


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_SWEEP.read_text(encoding="utf-8"))


def _steps(job: str) -> list[dict[str, Any]]:
    return _workflow()["jobs"][job]["steps"]


def _step(job: str, name: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r} in {job!r}"
    return matches[0]


def _matrix_areas() -> list[str]:
    return list(_workflow()["jobs"]["area"]["strategy"]["matrix"]["area"])


def _area_source_modules(area: str) -> list[str]:
    """Return the dotted module names of an area's non-__init__ source files."""
    package = _PACKAGE / area
    modules = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        parts = path.relative_to(_PACKAGE).with_suffix("").parts
        modules.append(".".join([mutation_scope.PACKAGE_NAME, *parts]))
    return modules


def test_sweep_never_runs_on_pull_requests() -> None:
    triggers = _workflow().get("on", _workflow().get(True))

    assert set(triggers) == {"schedule", "workflow_dispatch"}


@pytest.mark.parametrize("area", _matrix_areas())
def test_every_sweep_area_maps_to_source_with_mutable_functions(area: str) -> None:
    """An area whose filter reaches only an __init__.py has no mutants to sweep.

    That is the nightly failure this guards: mutmut reports "nothing matches",
    the runner exits 86, and the area job fails after the coverage run.
    """
    scope = mutation_runner._source_paths_for_mutant_names([f"osm_polygon_website_tag.{area}.*"])
    assert any(path.name != "__init__.py" for path in scope), area

    filters = [
        mutant_filter
        for module in _area_source_modules(area)
        for mutant_filter in mutation_scope.module_function_filters(module, root=_ROOT) or []
    ]
    assert filters, f"area {area!r} has no mutable function outside __init__.py"


def test_merge_requires_a_result_artifact_from_every_matrix_area() -> None:
    run = _step("merge", "Require a result artifact from every area")["run"]
    match = _AREA_LIST.search(run)

    assert match is not None
    assert match.group(1).split() == _matrix_areas()


def test_area_results_come_from_mutmut_without_masking_its_failure() -> None:
    run = _step("area", "Collect the area's unverified mutants")["run"]

    assert "|| true" not in run
    assert "mutmut results --all true >" in run


def test_nightly_publishes_the_baseline_diff_and_still_fails_on_growth() -> None:
    merge = _step("merge", "Merge the areas into a candidate baseline")["run"]
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
