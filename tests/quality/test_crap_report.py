"""Contract tests for the deterministic CRAP quality report command."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPORT = Path(__file__).parents[2] / "scripts" / "quality" / "crap_report.py"
TARGET = Path(__file__).parents[2] / "src" / "osm_polygon_website_tag" / "domain" / "tags.py"


def _coverage_file(tmp_path: Path, percent: float) -> Path:
    """Coverage for every function of ``TARGET``, all at ``percent``."""
    from radon.complexity import cc_visit

    entries = {
        f"{block.name}@{block.lineno}": {
            "start_line": block.lineno,
            "summary": {"percent_covered": percent},
        }
        for block in cc_visit(TARGET.read_text(encoding="utf-8"))
        if block.__class__.__name__ != "Class"
    }
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps({"files": {str(TARGET): {"functions": entries}}}),
        encoding="utf-8",
    )
    return path


def _run_report(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPORT), "--path", str(TARGET), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_report_for(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPORT), "--path", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_crap_report_passes_a_well_covered_function(tmp_path: Path) -> None:
    result = _run_report(
        "--coverage-json",
        str(_coverage_file(tmp_path, 100.0)),
        "--max-crap",
        "30",
    )

    assert result.returncode == 0
    assert "normalize_value" in result.stdout
    assert "CRAP" in result.stdout


def test_crap_report_defaults_to_a_strict_six_threshold(tmp_path: Path) -> None:
    result = _run_report("--coverage-json", str(_coverage_file(tmp_path, 0.0)))

    assert result.returncode == 1
    assert "6.00" in result.stderr


def test_crap_report_fails_when_threshold_is_reached(tmp_path: Path) -> None:
    result = _run_report(
        "--coverage-json",
        str(_coverage_file(tmp_path, 0.0)),
        "--max-crap",
        "5",
    )

    assert result.returncode == 1
    assert "normalize_value" in result.stdout
    assert "at or above" in result.stderr


def test_crap_report_treats_threshold_as_an_exclusive_upper_bound(tmp_path: Path) -> None:
    result = _run_report(
        "--coverage-json",
        str(_coverage_file(tmp_path, 0.0)),
        "--max-crap",
        "6",
    )

    assert result.returncode == 1
    assert "at or above" in result.stderr


def test_crap_report_expands_a_directory_in_deterministic_order(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "zeta.py").write_text("def zeta():\n    return 1\n", encoding="utf-8")
    (source_dir / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "files": {
                    str(source_dir / name): {
                        "functions": {name: {"start_line": 1, "summary": {"percent_covered": 100}}}
                    }
                    for name in ("zeta.py", "alpha.py")
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run_report_for(
        source_dir,
        "--coverage-json",
        str(coverage),
        "--max-crap",
        "3",
    )

    assert result.returncode == 0
    lines = [line for line in result.stdout.splitlines() if line.endswith("return 1")]
    assert lines == []
    alpha_index = result.stdout.index("alpha")
    zeta_index = result.stdout.index("zeta")
    assert alpha_index < zeta_index


def test_crap_report_counts_class_methods_once_not_as_classes(tmp_path: Path) -> None:
    source = tmp_path / "progress.py"
    source.write_text(
        "class ProgressReporter:\n    def __call__(self) -> None:\n        return None\n",
        encoding="utf-8",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "files": {
                    str(source): {
                        "functions": {
                            "__call__": {"start_line": 2, "summary": {"percent_covered": 100}}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = _run_report_for(
        source,
        "--coverage-json",
        str(coverage),
        "--max-crap",
        "1000",
    )

    assert result.returncode == 0
    assert "ProgressReporter" not in result.stdout
    assert result.stdout.count("__call__") == 1


def test_production_function_complexity_is_shallow() -> None:
    from radon.complexity import cc_visit

    source_root = Path(__file__).parents[2] / "src" / "osm_polygon_website_tag"
    failures = []
    for path in sorted(source_root.rglob("*.py")):
        for block in cc_visit(path.read_text(encoding="utf-8")):
            if block.__class__.__name__ == "Class":
                continue
            if block.complexity > 5:
                failures.append(f"{path}:{block.lineno} {block.name}={block.complexity}")

    assert failures == []


_DECORATED_SOURCE = """import functools


@functools.cache
def cached(x):
    def inner(y):
        if y:
            return 1
        return 2
    return inner(x)


class Box:
    @property
    def value(self):
        return 1

    @staticmethod
    def make(a):
        return a
"""


def _module(tmp_path: Path) -> Path:
    path = tmp_path / "mod.py"
    path.write_text(_DECORATED_SOURCE, encoding="utf-8")
    return path


def _coverage_for(path: Path, functions: dict[str, tuple[int, float]], tmp_path: Path) -> Path:
    report = tmp_path / "cov.json"
    entries = {
        name: {"start_line": line, "summary": {"percent_covered": percent}}
        for name, (line, percent) in functions.items()
    }
    report.write_text(json.dumps({"files": {str(path): {"functions": entries}}}), encoding="utf-8")
    return report


_ALL_COVERED = {
    "cached": (5, 100.0),
    "cached.inner": (6, 100.0),
    "Box.value": (15, 100.0),
    "Box.make": (19, 100.0),
}


def test_decorated_methods_and_nested_closures_are_each_scored_once(tmp_path: Path) -> None:
    module = _module(tmp_path)
    report = _coverage_for(module, _ALL_COVERED, tmp_path)

    result = _run_report_for(module, "--coverage-json", str(report))

    assert result.returncode == 0
    listed = [line.split()[0] for line in result.stdout.splitlines()[2:]]
    assert sorted(listed) == sorted(f"{module}:{line}" for line in (5, 6, 15, 19))


def test_an_uncovered_closure_is_scored_and_fails_the_gate(tmp_path: Path) -> None:
    module = _module(tmp_path)
    report = _coverage_for(module, {**_ALL_COVERED, "cached.inner": (6, 0.0)}, tmp_path)

    result = _run_report_for(module, "--coverage-json", str(report), "--max-crap", "5")

    assert result.returncode == 1
    assert f"{module}:6" in result.stdout


def test_a_function_without_a_coverage_entry_is_an_error_not_zero_percent(tmp_path: Path) -> None:
    module = _module(tmp_path)
    functions = {name: entry for name, entry in _ALL_COVERED.items() if name != "Box.make"}
    report = _coverage_for(module, functions, tmp_path)

    result = _run_report_for(module, "--coverage-json", str(report))

    assert result.returncode == 2
    assert f"{module}:19 make has no coverage entry" in result.stderr


def test_a_file_missing_from_the_coverage_report_is_an_error(tmp_path: Path) -> None:
    module = _module(tmp_path)
    report = tmp_path / "cov.json"
    report.write_text(json.dumps({"files": {}}), encoding="utf-8")

    result = _run_report_for(module, "--coverage-json", str(report))

    assert result.returncode == 2
    assert f"{module} is missing from the coverage report" in result.stderr


def test_coverage_without_per_function_data_is_an_error(tmp_path: Path) -> None:
    module = _module(tmp_path)
    report = tmp_path / "cov.json"
    report.write_text(json.dumps({"files": {str(module): {"summary": {}}}}), encoding="utf-8")

    result = _run_report_for(module, "--coverage-json", str(report))

    assert result.returncode == 2
    assert "coverage 7.5 or newer" in result.stderr
