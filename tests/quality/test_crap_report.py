"""Contract tests for the deterministic CRAP quality report command."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPORT = Path(__file__).parents[2] / "scripts" / "quality" / "crap_report.py"
TARGET = Path(__file__).parents[2] / "src" / "osm_polygon_website_tag" / "domain" / "tags.py"


def _coverage_file(tmp_path: Path, percent: float) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps(
            {
                "files": {
                    "src/osm_polygon_website_tag/domain/tags.py": {
                        "functions": {
                            "normalize_value": {
                                "summary": {"percent_covered": percent},
                                "start_line": 42,
                            }
                        }
                    }
                }
            }
        ),
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
    coverage.write_text('{"files": {}}', encoding="utf-8")

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
    coverage = _coverage_file(tmp_path, 100.0)
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
