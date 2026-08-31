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

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final, cast

MUTANTS_ROOT = Path("mutants")
_COVERAGE_FILE = ".mutmut-coverage"
_STATS_FILE = ".mutmut-stats-child.json"
_MPLCONFIGDIR: Final = (
    Path(tempfile.gettempdir()) / f"osm-polygon-website-tag-mutmut-mplconfig-{os.getpid()}"
)


def _project_root() -> Path:
    """Return the original checkout root from either source copy."""
    source_root = Path(__file__).resolve().parents[2]
    if source_root.name == MUTANTS_ROOT.name:
        return source_root.parent
    return source_root


def _mutants_directory() -> Path:
    """Return the absolute mutmut checkout independent of the current directory."""
    return _project_root() / MUTANTS_ROOT.name


def _stats_output_path() -> Path:
    """Return the temporary path used to transfer stats from the child."""
    return _mutants_directory() / _STATS_FILE


def _with_isolated_caches(environment: dict[str, str]) -> dict[str, str]:
    """Add writable, stable caches for subprocesses that load Matplotlib."""
    _MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)
    environment["MPLCONFIGDIR"] = str(_MPLCONFIGDIR)
    return environment


def _mutant_environment() -> dict[str, str]:
    """Return an environment that imports the isolated mutated source first."""
    environment = os.environ.copy()
    mutants_root = _mutants_directory()
    source_path = str(mutants_root / "src")
    root_path = str(mutants_root)
    environment["PYTHONPATH"] = os.pathsep.join((source_path, root_path))
    return _with_isolated_caches(environment)


def _coverage_environment(project_root: Path) -> dict[str, str]:
    """Return an environment that collects coverage from the original source."""
    environment = os.environ.copy()
    mutants_root = _mutants_directory()
    forbidden = {
        str(mutants_root / "src"),
        str(mutants_root),
    }
    existing = [
        path
        for path in environment.get("PYTHONPATH", "").split(os.pathsep)
        if path and str(Path(path).resolve()) not in forbidden
    ]
    environment["PYTHONPATH"] = os.pathsep.join(
        (str((project_root / "src").resolve()), str(project_root.resolve()), *existing)
    )
    environment["MUTANT_UNDER_TEST"] = ""
    return _with_isolated_caches(environment)


def _pytest_command(runner: Any, tests: Iterable[str]) -> list[str]:
    """Build mutmut's normal pytest command without invoking pytest in-process."""
    params = ["--rootdir=.", "--tb=native"]
    params.extend(runner._pytest_args_regular_run(tests))
    params.extend(runner._pytest_add_cli_args)
    return [sys.executable, "-m", "pytest", *params]


def _run_coverage(runner: Any, source_files: Iterable[Path]) -> dict[str, set[int]]:
    """Collect source coverage in a fresh process and return mutmut's mapping."""
    import coverage

    project_root = _project_root()
    data_path = _mutants_directory() / _COVERAGE_FILE
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
            cwd=Path.cwd(),
            env=_coverage_environment(project_root),
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
            original = (project_root / source_file).resolve()
            target = _mutants_directory() / source_file
            covered[str(target)] = set(data.lines(str(original)) or [])
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
        cwd=_mutants_directory(),
        env=_mutant_environment(),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode


def _run_stats(runner: Any, tests: Iterable[str]) -> int:
    """Collect mutmut test associations in a fresh interpreter."""
    del runner
    output_path = _stats_output_path()
    output_path.unlink(missing_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--stats-child",
        str(output_path),
        json.dumps(list(tests)),
    ]
    environment = _mutant_environment()
    environment["MUTANT_UNDER_TEST"] = "stats"
    environment["PY_IGNORE_IMPORTMISMATCH"] = "1"
    try:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=Path.cwd().resolve(),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            _merge_stats(output_path)
        else:
            detail = (result.stdout + result.stderr).strip()
            if detail:
                print(detail[-2000:])
        return result.returncode
    finally:
        output_path.unlink(missing_ok=True)


def _merge_stats(output_path: Path) -> None:
    """Merge a successful child stats payload into mutmut's parent state."""
    import mutmut
    from mutmut.state import state

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    for function_name, test_names in payload["tests_by_mangled_function_name"].items():
        mutmut.tests_by_mangled_function_name[function_name].update(test_names)
    for test_name, duration in payload["duration_by_test"].items():
        mutmut.duration_by_test[test_name] = float(duration)
    for function_name, callers in payload["function_dependencies"].items():
        state().function_dependencies[function_name].update(callers)


def _run_stats_child(output_path: Path, tests: Iterable[str]) -> int:
    """Run pytest's mutmut stats collector and serialize its parent payload."""
    import mutmut
    import mutmut.__main__ as mutmut_main
    from mutmut.state import state
    from mutmut.utils.file_utils import change_cwd
    from mutmut.utils.format_utils import strip_prefix

    mutmut._reset_globals()

    class StatsCollector:
        def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
            del location
            mutmut.duration_by_test[nodeid] = 0.0

        def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
            del nextitem
            for function in mutmut._stats:
                mutmut.tests_by_mangled_function_name[function].add(
                    strip_prefix(item._nodeid, prefix="mutants/")
                )
            mutmut._stats.clear()

        def pytest_runtest_makereport(self, item: Any, call: Any) -> None:
            mutmut.duration_by_test[item.nodeid] += call.duration

    runner = mutmut_main.PytestRunner()
    with change_cwd(_mutants_directory()):
        exit_code = runner.execute_pytest(
            runner._pytest_args_regular_run(tests), plugins=[StatsCollector()]
        )
    payload = {
        "tests_by_mangled_function_name": {
            name: sorted(test_names)
            for name, test_names in mutmut.tests_by_mangled_function_name.items()
        },
        "duration_by_test": dict(mutmut.duration_by_test),
        "function_dependencies": {
            name: sorted(callers) for name, callers in state().function_dependencies.items()
        },
    }
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    return exit_code


def main() -> None:
    """Install the isolated hooks and delegate argument parsing to mutmut."""
    if len(sys.argv) == 4 and sys.argv[1] == "--stats-child":
        output_path = Path(sys.argv[2])
        tests = json.loads(sys.argv[3])
        if not isinstance(tests, list) or not all(isinstance(test, str) for test in tests):
            raise ValueError("stats child tests must be a JSON array of strings")
        raise SystemExit(_run_stats_child(output_path, tests))

    import mutmut.__main__ as mutmut_main

    def gather_coverage(runner: Any, source_files: Iterable[Path]) -> dict[str, set[int]]:
        return _gather_coverage(runner, source_files)

    def run_tests(self: Any, *, mutant_name: str | None, tests: Iterable[str]) -> int:
        return _run_tests(self, mutant_name=mutant_name, tests=tests)

    def run_stats(self: Any, *, tests: Iterable[str]) -> int:
        return _run_stats(self, tests)

    mutmut_main.gather_coverage = cast(Any, gather_coverage)
    mutmut_main.PytestRunner.run_tests = cast(Any, run_tests)
    mutmut_main.PytestRunner.run_stats = cast(Any, run_stats)
    sys.argv[0] = "mutmut"
    mutmut_main.cli()


if __name__ == "__main__":
    main()
