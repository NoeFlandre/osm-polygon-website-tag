#!/usr/bin/env python3
"""Run mutmut without reloading native extensions in one Python process.

The project exercises native extensions (DuckDB, pyarrow, and osmium).  Mutmut's
default pytest runner calls ``pytest.main`` repeatedly in the same interpreter;
on macOS that can unload a native extension and make later test processes fail
with errors such as ``_duckdb._sqltypes is not a package``.  This adapter keeps
mutmut's mutation model, but runs coverage once and every mutant test in a
fresh child interpreter.

The command accepts the same arguments as ``mutmut run``.  It is intentionally
small and depends only on mutmut's public command entry point plus the two
runner hooks that are stable in the supported mutmut 3.x range.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

MUTANTS_ROOT = Path("mutants")
_COVERAGE_FILE = ".mutmut-coverage"


def _mutant_environment() -> dict[str, str]:
    """Return an environment that imports the isolated mutated source first."""
    environment = os.environ.copy()
    source_path = str((MUTANTS_ROOT / "src").resolve())
    root_path = str(MUTANTS_ROOT.resolve())
    environment["PYTHONPATH"] = os.pathsep.join((source_path, root_path))
    return environment


def _pytest_command(runner: Any, tests: Iterable[str]) -> list[str]:
    """Build mutmut's normal pytest command without invoking pytest in-process."""
    params = ["--rootdir=.", "--tb=native"]
    params.extend(runner._pytest_args_regular_run(tests))
    params.extend(runner._pytest_add_cli_args)
    return [sys.executable, "-m", "pytest", *params]


def _run_coverage(runner: Any, source_files: Iterable[Path]) -> dict[str, set[int]]:
    """Collect source coverage in a fresh process and return mutmut's mapping."""
    import coverage

    data_path = (MUTANTS_ROOT / _COVERAGE_FILE).resolve()
    command = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        f"--data-file={data_path}",
        "--source=src",
        "-m",
        "pytest",
        *_pytest_command(runner, ())[3:],
    ]
    try:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=MUTANTS_ROOT,
            env=_mutant_environment(),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip()
            raise RuntimeError(f"coverage test run failed ({result.returncode}): {detail[-2000:]}")

        loaded = coverage.Coverage(data_file=str(data_path))
        loaded.load()
        data = loaded.get_data()
        covered: dict[str, set[int]] = {}
        for source_file in source_files:
            target = (MUTANTS_ROOT / source_file).resolve()
            covered[str(target)] = set(data.lines(str(target)) or [])
        return covered
    finally:
        data_path.unlink(missing_ok=True)


def _gather_coverage(runner: Any, source_files: Iterable[Path]) -> dict[str, set[int]]:
    """Adapter matching mutmut's coverage hook signature."""
    return _run_coverage(runner, tuple(source_files))


def _run_tests(runner: Any, *, mutant_name: str | None, tests: Iterable[str]) -> int:
    """Run one mutant's selected tests in a new interpreter."""
    del mutant_name
    result = subprocess.run(  # noqa: S603
        _pytest_command(runner, tests),
        cwd=MUTANTS_ROOT,
        env=_mutant_environment(),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode


def main() -> None:
    """Install the isolated hooks and delegate argument parsing to mutmut."""
    import mutmut.__main__ as mutmut_main

    def gather_coverage(runner: Any, source_files: Iterable[Path]) -> dict[str, set[int]]:
        return _gather_coverage(runner, source_files)

    def run_tests(self: Any, *, mutant_name: str | None, tests: Iterable[str]) -> int:
        return _run_tests(self, mutant_name=mutant_name, tests=tests)

    mutmut_main.gather_coverage = cast(Any, gather_coverage)
    mutmut_main.PytestRunner.run_tests = cast(Any, run_tests)
    sys.argv[0] = "mutmut"
    mutmut_main.cli()


if __name__ == "__main__":
    main()
