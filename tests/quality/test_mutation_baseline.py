"""Tests for writing the mutation baseline, its header count, and its reviewable diff."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.quality import mutation_baseline
from scripts.quality.mutation_gate import read_baseline

_ROOT = Path(__file__).resolve().parents[2]
_COMMITTED_BASELINE = _ROOT / "docs" / "quality" / "mutation-baseline.txt"
_REVIEW = _ROOT / "docs" / "quality" / "duplication-review.md"
_HEADER_COUNT = re.compile(r"^# Recorded mutants: (?P<count>\d+)$", re.MULTILINE)
_NAME_A = "osm_polygon_website_tag.pipeline.a.x_f__mutmut_1"
_NAME_B = "osm_polygon_website_tag.pipeline.a.x_g__mutmut_2"
_NAME_C = "osm_polygon_website_tag.pipeline.a.x_h__mutmut_3"


def _results(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "results.txt"
    path.write_text("".join(f"{name}: survived\n" for name in names), encoding="utf-8")
    return path


def _baseline(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "baseline.txt"
    path.write_text(mutation_baseline.render(names), encoding="utf-8")
    return path


def test_render_header_count_equals_the_number_of_entries(tmp_path: Path) -> None:
    text = mutation_baseline.render([_NAME_B, _NAME_A, _NAME_A])
    path = tmp_path / "baseline.txt"
    path.write_text(text, encoding="utf-8")

    header = _HEADER_COUNT.search(text)

    assert header is not None
    assert int(header.group("count")) == len(read_baseline(path)) == 2


def test_committed_baseline_header_count_matches_its_entries() -> None:
    header = _HEADER_COUNT.search(_COMMITTED_BASELINE.read_text(encoding="utf-8"))

    assert header is not None
    assert int(header.group("count")) == len(read_baseline(_COMMITTED_BASELINE))


def test_review_states_the_committed_baseline_count_and_web_fetch_status() -> None:
    """The duplication review must describe the committed baseline, not a guess."""
    entries = read_baseline(_COMMITTED_BASELINE)
    review = _REVIEW.read_text(encoding="utf-8")

    assert f"holds {len(entries)} entries and no `web.web_fetch` entry" in review
    assert not any(".web_fetch." in name for name in entries)


def test_growth_writes_candidate_and_a_diff_of_the_added_survivor(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path, _NAME_A)
    results = _results(tmp_path, _NAME_A, _NAME_B)
    candidate = tmp_path / "candidate.txt"
    diff = tmp_path / "baseline.diff"

    status = mutation_baseline.main(
        [
            "--results",
            str(results),
            "--baseline",
            str(baseline),
            "--output",
            str(candidate),
            "--diff",
            str(diff),
            "--fail-on-growth",
        ]
    )

    assert status == 1
    assert read_baseline(candidate) == {_NAME_A, _NAME_B}
    diff_text = diff.read_text(encoding="utf-8")
    assert f"--- {baseline}" in diff_text
    assert f"+++ {candidate}" in diff_text
    assert f"+{_NAME_B}\n" in diff_text
    assert f"-{_NAME_B}" not in diff_text


def test_healed_entries_show_as_removals_without_failing(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path, _NAME_A, _NAME_C)
    results = _results(tmp_path, _NAME_A)
    candidate = tmp_path / "candidate.txt"
    diff = tmp_path / "baseline.diff"

    status = mutation_baseline.main(
        [
            "--results",
            str(results),
            "--baseline",
            str(baseline),
            "--output",
            str(candidate),
            "--diff",
            str(diff),
            "--fail-on-growth",
        ]
    )

    assert status == 0
    assert read_baseline(candidate) == {_NAME_A}
    assert f"-{_NAME_C}\n" in diff.read_text(encoding="utf-8")


def test_missing_baseline_counts_every_survivor_as_growth(tmp_path: Path) -> None:
    baseline = tmp_path / "absent.txt"
    results = _results(tmp_path, _NAME_A)
    candidate = tmp_path / "candidate.txt"
    diff = tmp_path / "nested" / "baseline.diff"

    status = mutation_baseline.main(
        [
            "--results",
            str(results),
            "--baseline",
            str(baseline),
            "--output",
            str(candidate),
            "--diff",
            str(diff),
            "--fail-on-growth",
        ]
    )

    assert status == 1
    assert f"+{_NAME_A}\n" in diff.read_text(encoding="utf-8")


def test_unified_diff_of_identical_baselines_is_empty() -> None:
    text = mutation_baseline.render([_NAME_A])

    assert mutation_baseline.unified_diff(text, text, current_name="a", candidate_name="b") == ""
