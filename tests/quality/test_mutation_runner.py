"""Tests for the isolated mutmut subprocess adapter."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from scripts.quality import mutation_runner


def _runner() -> SimpleNamespace:
    return SimpleNamespace(
        _pytest_args_regular_run=lambda tests: ["-x", "-q", *tests],
        _pytest_add_cli_args=["--ignore=tests/architecture"],
    )


def test_mutant_environment_prioritizes_isolated_source(monkeypatch) -> None:
    monkeypatch.setenv("MUTMUT_TEST_SENTINEL", "kept")

    environment = mutation_runner._mutant_environment()

    assert environment["MUTMUT_TEST_SENTINEL"] == "kept"
    assert environment["PYTHONPATH"].split(":")[:2] == [
        str((Path("mutants") / "src").resolve()),
        str(Path("mutants").resolve()),
    ]
    assert environment["MPLCONFIGDIR"] == str(mutation_runner._MPLCONFIGDIR)


def test_pytest_command_preserves_mutmut_selection_and_project_flags() -> None:
    command = mutation_runner._pytest_command(_runner(), ["tests/example.py"])

    assert command[:3] == [mutation_runner.sys.executable, "-m", "pytest"]
    assert "--rootdir=." in command
    assert "tests/example.py" in command
    assert "--ignore=tests/architecture" in command


def test_coverage_environment_prefers_original_checkout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(("external", str((Path("mutants") / "src").resolve()), "tail")),
    )

    environment = mutation_runner._coverage_environment(tmp_path)

    paths = environment["PYTHONPATH"].split(os.pathsep)
    assert paths[:2] == [str((tmp_path / "src").resolve()), str(tmp_path.resolve())]
    assert str((Path("mutants") / "src").resolve()) not in paths
    assert environment["MUTANT_UNDER_TEST"] == ""
    assert environment["MPLCONFIGDIR"] == str(mutation_runner._MPLCONFIGDIR)


def test_run_coverage_maps_original_lines_to_mutant_paths(monkeypatch) -> None:
    import coverage

    captured: dict[str, object] = {}
    source_file = Path("src/example.py")

    class FakeCoverage:
        def __init__(self, **kwargs):
            captured["coverage_kwargs"] = kwargs

        def load(self) -> None:
            return None

        def get_data(self):
            original = str((Path.cwd() / source_file).resolve())
            return SimpleNamespace(lines=lambda path: {11} if path == original else {99})

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(coverage, "Coverage", FakeCoverage)
    monkeypatch.setattr(mutation_runner.subprocess, "run", fake_run)

    result = mutation_runner._run_coverage(_runner(), [source_file])

    mutant_path = str((Path("mutants") / source_file).resolve())
    assert result == {mutant_path: {11}}
    assert captured["cwd"] == Path.cwd().resolve()
    environment = cast(dict[str, str], captured["env"])
    command = cast(list[str], captured["command"])
    assert environment["MUTANT_UNDER_TEST"] == ""
    assert "--source=src" in command
    assert not (mutation_runner.MUTANTS_ROOT / mutation_runner._COVERAGE_FILE).exists()
    assert "coverage" in sys.modules


def test_run_tests_returns_child_exit_code(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(mutation_runner.subprocess, "run", fake_run)

    result = mutation_runner._run_tests(
        _runner(),
        mutant_name="example__mutmut_1",
        tests=["tests/example.py"],
    )

    assert result == 7
    assert captured["cwd"] == mutation_runner.MUTANTS_ROOT
    assert captured["check"] is False
    assert captured["stdout"] is mutation_runner.subprocess.DEVNULL
    assert captured["stderr"] is mutation_runner.subprocess.DEVNULL


def test_run_stats_merges_fresh_child_payload(monkeypatch, tmp_path: Path) -> None:
    import mutmut
    from mutmut.state import state

    payload = {
        "tests_by_mangled_function_name": {"function": ["tests/test_one.py::test_one"]},
        "duration_by_test": {"tests/test_one.py::test_one": 0.25},
        "function_dependencies": {"function": ["caller"]},
    }
    output = tmp_path / "stats.json"

    def fake_run(command, **kwargs):
        del command
        del kwargs
        output.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mutation_runner, "_stats_output_path", lambda: output)
    monkeypatch.setattr(mutation_runner.subprocess, "run", fake_run)
    mutmut.tests_by_mangled_function_name.clear()
    mutmut.duration_by_test.clear()
    state().function_dependencies.clear()

    assert mutation_runner._run_stats(_runner(), []) == 0
    assert mutmut.tests_by_mangled_function_name["function"] == {"tests/test_one.py::test_one"}
    assert mutmut.duration_by_test["tests/test_one.py::test_one"] == 0.25
    assert state().function_dependencies["function"] == {"caller"}


def test_run_stats_invokes_child_and_removes_transfer_file(monkeypatch, tmp_path: Path) -> None:
    import mutmut

    captured: dict[str, object] = {}
    output = tmp_path / "stats.json"
    payload = {
        "tests_by_mangled_function_name": {},
        "duration_by_test": {},
        "function_dependencies": {},
    }

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        output.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mutation_runner, "_stats_output_path", lambda: output)
    monkeypatch.setattr(mutation_runner.subprocess, "run", fake_run)
    mutmut.tests_by_mangled_function_name.clear()
    mutmut.duration_by_test.clear()

    assert mutation_runner._run_stats(_runner(), ["tests/test_one.py::test_one"]) == 0
    command = captured["command"]
    assert isinstance(command, list)
    assert command[2] == "--stats-child"
    assert captured["cwd"] == Path.cwd().resolve()
    assert captured["check"] is False
    assert captured["capture_output"] is True
    assert captured["text"] is True
    environment = cast(dict[str, str], captured["env"])
    assert environment["MUTANT_UNDER_TEST"] == "stats"
    assert environment["PY_IGNORE_IMPORTMISMATCH"] == "1"
    assert not output.exists()


def test_run_stats_reports_child_output_and_cleans_up_after_failure(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    output = tmp_path / "stats.json"

    def fake_run(command, **kwargs):
        del command
        del kwargs
        output.write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=7, stdout="child stdout", stderr="child stderr")

    monkeypatch.setattr(mutation_runner, "_stats_output_path", lambda: output)
    monkeypatch.setattr(mutation_runner.subprocess, "run", fake_run)

    assert mutation_runner._run_stats(_runner(), []) == 7
    assert "child stdoutchild stderr" in capsys.readouterr().out
    assert not output.exists()


def test_stats_child_serializes_mutmut_state(monkeypatch, tmp_path: Path) -> None:
    import mutmut
    import mutmut.__main__ as mutmut_main
    from mutmut.state import state

    class FakeRunner:
        def _pytest_args_regular_run(self, tests):
            return [*tests]

        def execute_pytest(self, params, **kwargs):
            assert params == ["tests/test_one.py::test_one"]
            assert len(kwargs["plugins"]) == 1
            mutmut.tests_by_mangled_function_name["function"].add(params[0])
            mutmut.duration_by_test[params[0]] = 0.25
            state().function_dependencies["function"].add("caller")
            return 0

    monkeypatch.setattr(mutmut_main, "PytestRunner", FakeRunner)
    mutmut.tests_by_mangled_function_name.clear()
    mutmut.duration_by_test.clear()
    state().function_dependencies.clear()
    output = tmp_path / "stats.json"

    assert mutation_runner._run_stats_child(output, ["tests/test_one.py::test_one"]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "tests_by_mangled_function_name": {"function": ["tests/test_one.py::test_one"]},
        "duration_by_test": {"tests/test_one.py::test_one": 0.25},
        "function_dependencies": {"function": ["caller"]},
    }
