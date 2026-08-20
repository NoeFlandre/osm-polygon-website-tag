"""Tests for the isolated mutmut subprocess adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_pytest_command_preserves_mutmut_selection_and_project_flags() -> None:
    command = mutation_runner._pytest_command(_runner(), ["tests/example.py"])

    assert command[:3] == [mutation_runner.sys.executable, "-m", "pytest"]
    assert "--rootdir=." in command
    assert "tests/example.py" in command
    assert "--ignore=tests/architecture" in command


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
