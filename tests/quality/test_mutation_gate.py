"""Tests for the baseline-aware mutation gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.quality import mutation_gate

_RESULTS = """
To apply a mutant on disk:
    osm_polygon_website_tag.pipeline.sat.x_select_device__mutmut_1: killed
    osm_polygon_website_tag.web.web_fetch.x__probe__mutmut_3: survived
    osm_polygon_website_tag.domain.geometry.x__area_bucket__mutmut_2: survived
    osm_polygon_website_tag.pipeline.enrich.x__retry__mutmut_7: no tests
    osm_polygon_website_tag.pipeline.analyze.x__slow__mutmut_1: timeout
    osm_polygon_website_tag.reporting.card.x__render__mutmut_9: not checked
"""


def _write(tmp_path: Path, results: str = _RESULTS, baseline: str = "") -> tuple[Path, Path]:
    results_path = tmp_path / "results.txt"
    baseline_path = tmp_path / "baseline.txt"
    results_path.write_text(results, encoding="utf-8")
    baseline_path.write_text(baseline, encoding="utf-8")
    return results_path, baseline_path


def test_unverified_mutants_ignores_killed_and_unchecked() -> None:
    assert mutation_gate.unverified_mutants(_RESULTS.splitlines()) == [
        "osm_polygon_website_tag.web.web_fetch.x__probe__mutmut_3",
        "osm_polygon_website_tag.domain.geometry.x__area_bucket__mutmut_2",
        "osm_polygon_website_tag.pipeline.enrich.x__retry__mutmut_7",
        "osm_polygon_website_tag.pipeline.analyze.x__slow__mutmut_1",
    ]


def test_every_documented_verdict_counts_as_unverified() -> None:
    lines = [
        f"    module.x_f__mutmut_1: {verdict}" for verdict in mutation_gate.UNVERIFIED_VERDICTS
    ]

    assert len(mutation_gate.unverified_mutants(lines)) == len(mutation_gate.UNVERIFIED_VERDICTS)
    assert mutation_gate.UNVERIFIED_VERDICTS == (
        "survived",
        "no tests",
        "timeout",
        "suspicious",
        "segfault",
        "check was interrupted",
    )


def test_killed_mutants_are_collected() -> None:
    assert mutation_gate.killed_mutants(_RESULTS.splitlines()) == {
        "osm_polygon_website_tag.pipeline.sat.x_select_device__mutmut_1"
    }


def test_baseline_ignores_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "baseline.txt"
    path.write_text("# a note\n\n  module.x_f__mutmut_1  \n", encoding="utf-8")

    assert mutation_gate.read_baseline(path) == {"module.x_f__mutmut_1"}
    assert mutation_gate.read_baseline(tmp_path / "missing.txt") == set()


def test_a_survivor_outside_the_baseline_fails_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results, baseline = _write(tmp_path)

    code = mutation_gate.main(["--results", str(results), "--baseline", str(baseline)])

    captured = capsys.readouterr()
    assert code == 1
    assert "4 unverified mutant(s) outside the baseline" in captured.err
    assert "web_fetch.x__probe__mutmut_3" in captured.err


def test_a_fully_recorded_backlog_passes_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = "\n".join(mutation_gate.unverified_mutants(_RESULTS.splitlines()))
    results, baseline_path = _write(tmp_path, baseline=baseline)

    code = mutation_gate.main(["--results", str(results), "--baseline", str(baseline_path)])

    assert code == 0
    assert "4 baseline hit(s)" in capsys.readouterr().out


def test_one_new_survivor_still_fails_a_recorded_backlog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded = mutation_gate.unverified_mutants(_RESULTS.splitlines())
    baseline = "\n".join(recorded[1:])
    results, baseline_path = _write(tmp_path, baseline=baseline)

    code = mutation_gate.main(["--results", str(results), "--baseline", str(baseline_path)])

    assert code == 1
    assert "1 unverified mutant(s) outside the baseline" in capsys.readouterr().err


def test_a_healed_baseline_entry_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = "\n".join(
        [
            *mutation_gate.unverified_mutants(_RESULTS.splitlines()),
            "osm_polygon_website_tag.pipeline.sat.x_select_device__mutmut_1",
        ]
    )
    results, baseline_path = _write(tmp_path, baseline=baseline)

    code = mutation_gate.main(["--results", str(results), "--baseline", str(baseline_path)])

    captured = capsys.readouterr()
    assert code == 0
    assert "1 baseline mutant(s) are now killed" in captured.out
    assert "sat.x_select_device__mutmut_1" in captured.out


def test_an_empty_scoped_run_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results, baseline = _write(tmp_path, results="    module.x_f__mutmut_1: not checked\n")

    code = mutation_gate.main(["--results", str(results), "--baseline", str(baseline)])

    assert code == 0
    assert "0 baseline hit(s)" in capsys.readouterr().out


def test_the_repository_baseline_is_sorted_and_documented() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "quality" / "mutation-baseline.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    comments = [line for line in lines if line.startswith("#")]
    entries = [line for line in lines if line and not line.startswith("#")]

    assert comments, "the baseline must explain what it records"
    assert entries == sorted(entries)
    assert len(entries) == len(set(entries))
    assert all(entry.startswith("osm_polygon_website_tag.") for entry in entries)


def test_baseline_generation_is_sorted_deduplicated_and_documented(tmp_path: Path) -> None:
    from scripts.quality import mutation_baseline

    results = tmp_path / "results.txt"
    results.write_text(
        "    b.x_f__mutmut_2: survived\n"
        "    a.x_f__mutmut_1: survived\n"
        "    a.x_f__mutmut_1: survived\n"
        "    c.x_f__mutmut_3: killed\n",
        encoding="utf-8",
    )
    baseline = tmp_path / "nested" / "baseline.txt"

    assert mutation_baseline.main(["--results", str(results), "--baseline", str(baseline)]) == 0

    lines = baseline.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line and not line.startswith("#")] == [
        "a.x_f__mutmut_1",
        "b.x_f__mutmut_2",
    ]
    assert "# Recorded mutants: 2" in lines
    assert mutation_gate.read_baseline(baseline) == {"a.x_f__mutmut_1", "b.x_f__mutmut_2"}


def test_baseline_generation_defaults_to_the_repository_file() -> None:
    from scripts.quality import mutation_baseline

    parser_default = mutation_baseline.main.__doc__
    assert parser_default
    assert mutation_baseline.render([]).endswith("\n")
    assert "# Recorded mutants: 0" in mutation_baseline.render([])


def test_scopes_limit_which_mutants_a_run_judges() -> None:
    assert mutation_gate.in_scope("a.b.x_f__mutmut_1", []) is True
    assert mutation_gate.in_scope("a.b.x_f__mutmut_1", ["a.b.*"]) is True
    assert mutation_gate.in_scope("a.b.x_f__mutmut_1", ["a.c.*"]) is False
    assert mutation_gate.in_scope("a.b.x_f__mutmut_1", ["a.c.*", "a.b.*"]) is True


def test_a_scoped_run_ignores_stale_verdicts_from_other_modules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verdicts persist in the workspace; a scoped run must judge its own scope."""
    results, baseline = _write(tmp_path)

    code = mutation_gate.main(
        [
            "--results",
            str(results),
            "--baseline",
            str(baseline),
            "--scope",
            "osm_polygon_website_tag.pipeline.enrich.*",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "1 unverified mutant(s) outside the baseline" in captured.err
    assert "enrich.x__retry__mutmut_7" in captured.err
    assert "web_fetch" not in captured.err


def test_a_scope_with_no_findings_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results, baseline = _write(tmp_path)

    code = mutation_gate.main(
        ["--results", str(results), "--baseline", str(baseline), "--scope", "other.package.*"]
    )

    assert code == 0
    assert "0 baseline hit(s)" in capsys.readouterr().out


def test_the_gate_lists_every_finding_not_a_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated list cannot be recorded in the baseline or acted on."""
    names = [f"osm_polygon_website_tag.m.x_f__mutmut_{i}" for i in range(60)]
    results, baseline = _write(
        tmp_path, results="".join(f"    {name}: survived\n" for name in names)
    )

    code = mutation_gate.main(["--results", str(results), "--baseline", str(baseline)])

    captured = capsys.readouterr()
    assert code == 1
    assert all(name in captured.err for name in names)
