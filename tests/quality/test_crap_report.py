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
