"""Architecture checks for the policy-aware Grid'5000 shell boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from osm_polygon_website_tag.pipeline.grid5000_bundle import (
    DEFAULT_GRID_LANGUAGE_BATCH_ROWS,
    DEFAULT_GRID_SENTENCE_BATCH_ROWS,
    DEFAULT_GRID_TIME_BUDGET_SECONDS,
)

SCRIPT_ROOT = Path("scripts/grid5000")
SCRIPT_NAMES = (
    "bootstrap_language_runtime.sh",
    "prepare_language_detection.sh",
    "run_language_detection.sh",
    "submit_language_detection.sh",
    "sync_language_detection.sh",
    "prepare_sentence_segmentation.sh",
    "run_sentence_segmentation.sh",
    "sync_sentence_segmentation.sh",
    "bootstrap_sentence_runtime.sh",
    "bootstrap_runtime.sh",
    "sync_bundle.sh",
)
# Sourced by the node job scripts, never executed or submitted itself.
ENV_SCRIPT = SCRIPT_ROOT / "_env.sh"
NODE_JOB_SCRIPTS = (
    "bootstrap_runtime.sh",
    "run_language_detection.sh",
    "run_sentence_segmentation.sh",
)
# The arguments `_env.sh` passes to `module`: the pinned runtime modules.
PINNED_MODULES = "load python/3.12.12 uv/0.10.12 expat/2.7.1"


def test_grid5000_scripts_are_executable() -> None:
    for name in SCRIPT_NAMES:
        path = SCRIPT_ROOT / name
        assert path.is_file()
        assert os.access(path, os.X_OK)


# Stand-ins for the cluster's `module` and `uv`. They record what a script asked for,
# so the tests check the environment a job receives rather than the script's text.
_MODULE_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$RECORD_DIR/module.txt"
"""
_UV_STUB = r"""#!/bin/sh
{
  printf 'cwd=%s\n' "$(pwd -P)"
  printf 'uv_cache_dir=%s\n' "$UV_CACHE_DIR"
  printf 'args=%s\n' "$*"
} > "$RECORD_DIR/uv.txt"
"""


def _run_node_script_with_stubs(
    tmp_path: Path,
    script: str,
    arguments: tuple[str, ...],
    extra_env: dict[str, str],
) -> tuple[str, dict[str, str]]:
    """Run one node script under the stub tools; return the module and uv calls it made."""
    bin_dir = tmp_path / "bin"
    record_dir = tmp_path / "record"
    bin_dir.mkdir()
    record_dir.mkdir()
    for tool, body in (("module", _MODULE_STUB), ("uv", _UV_STUB)):
        (bin_dir / tool).write_text(body, encoding="utf-8")
        (bin_dir / tool).chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "RECORD_DIR": str(record_dir),
        **extra_env,
    }

    result = subprocess.run(
        ["bash", str((SCRIPT_ROOT / script).resolve()), *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    modules = (record_dir / "module.txt").read_text(encoding="utf-8")
    uv_lines = (record_dir / "uv.txt").read_text(encoding="utf-8").splitlines()
    return modules, dict(line.split("=", 1) for line in uv_lines)


def test_only_the_shared_environment_loads_modules() -> None:
    for path in SCRIPT_ROOT.glob("*.sh"):
        if path != ENV_SCRIPT:
            assert "module load" not in path.read_text(), path.name


@pytest.mark.parametrize("script", NODE_JOB_SCRIPTS)
def test_node_job_scripts_load_the_pins_and_run_in_the_default_checkout(
    tmp_path: Path,
    script: str,
) -> None:
    job_dir = tmp_path / "job"
    checkout = job_dir / "checkout"
    checkout.mkdir(parents=True)
    arguments = ("language",) if script == "bootstrap_runtime.sh" else ()

    modules, uv = _run_node_script_with_stubs(
        tmp_path, script, arguments, {"GRID5000_JOB_DIR": str(job_dir)}
    )

    assert modules == f"{PINNED_MODULES}\n"
    assert uv["cwd"] == str(checkout.resolve())
    assert uv["uv_cache_dir"] == str(job_dir / "uv-cache")


def test_explicit_checkout_and_cache_variables_override_the_job_directory(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    cache = tmp_path / "cache"

    _, uv = _run_node_script_with_stubs(
        tmp_path,
        "bootstrap_runtime.sh",
        ("language",),
        {
            "GRID5000_JOB_DIR": str(tmp_path / "job"),
            "GRID5000_REPO_DIR": str(checkout),
            "GRID5000_UV_CACHE_DIR": str(cache),
        },
    )

    assert uv["cwd"] == str(checkout.resolve())
    assert uv["uv_cache_dir"] == str(cache)


@pytest.mark.parametrize(
    ("stage", "expected_args"),
    [
        ("language", "sync --locked --no-dev --python 3.12"),
        ("sentences", "sync --locked --no-dev --extra sentences --python 3.12"),
    ],
)
def test_bootstrap_syncs_the_locked_runtime_for_each_stage(
    tmp_path: Path,
    stage: str,
    expected_args: str,
) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "checkout").mkdir(parents=True)

    _, uv = _run_node_script_with_stubs(
        tmp_path, "bootstrap_runtime.sh", (stage,), {"GRID5000_JOB_DIR": str(job_dir)}
    )

    assert uv["args"] == expected_args


def test_submitted_job_scripts_keep_their_oar_headers() -> None:
    for name in (
        "bootstrap_runtime.sh",
        "bootstrap_language_runtime.sh",
        "bootstrap_sentence_runtime.sh",
        "run_language_detection.sh",
        "run_sentence_segmentation.sh",
    ):
        script = (SCRIPT_ROOT / name).read_text()
        assert "#OAR -l host=1/gpu=1,walltime=0:30" in script, name
        assert "#OAR -O OAR_%jobid%.out" in script, name
        assert "#OAR -E OAR_%jobid%.err" in script, name


def test_stage_wrappers_delegate_to_the_shared_scripts() -> None:
    for name, target, stage in (
        ("bootstrap_language_runtime.sh", "bootstrap_runtime.sh", "language"),
        ("bootstrap_sentence_runtime.sh", "bootstrap_runtime.sh", "sentences"),
        ("sync_language_detection.sh", "sync_bundle.sh", "language"),
        ("sync_sentence_segmentation.sh", "sync_bundle.sh", "sentences"),
    ):
        script = (SCRIPT_ROOT / name).read_text()
        assert target in script, name
        assert f" {stage}" in script, name


def test_submit_script_checks_policy_around_submission() -> None:
    script = (SCRIPT_ROOT / "submit_language_detection.sh").read_text()
    submit_call = 'oarsub "${oarsub_arguments[@]}"'

    assert script.count("usagepolicycheck -t") >= 2
    assert script.index("usagepolicycheck -t") < script.index(submit_call)
    assert script.rindex("usagepolicycheck -t") > script.index(submit_call)
    assert "walltime=0:30" in script
    assert "host=1/gpu=" in script
    assert '-q "$queue"' in script
    assert "GRID5000_GPUS:-1" in script
    assert "GRID5000_QUEUE:-abaca" in script
    assert "GRID5000_PROPERTIES:-" in script
    assert 'oarsub_arguments+=(-p "$properties")' in script
    assert "GRID5000_REPO_DIR" in script
    assert 'bundle_dir="${GRID5000_BUNDLE_DIR:-$job_dir/bundle}"' in script
    assert "scripts/grid5000/run_language_detection.sh" in script
    assert "OAR job id" in script
    assert "OAR_JOB_ID" in script
    assert "sed -nE" in script
    assert "active" in script.lower()
    assert 'export GRID5000_JOB_DIR="$job_dir"' in script
    assert 'export GRID5000_REPO_DIR="$repo_dir"' in script
    assert 'export GRID5000_BUNDLE_DIR="$bundle_dir"' in script
    assert 'export GRID5000_UV_CACHE_DIR="$uv_cache_dir"' in script
    assert 'cd "$job_dir"' in script


def test_reserved_node_runner_is_offline_and_has_a_cleanup_margin() -> None:
    script = (SCRIPT_ROOT / "run_language_detection.sh").read_text()

    assert "#OAR -l host=1/gpu=1,walltime=0:30" in script
    assert "grid5000_run_setup\n" in script
    assert '"${run_arguments[@]}"' in script
    assert "--offline" in script
    assert "python -m osm_polygon_website_tag.application.grid5000_runner" in script
    assert "osm-polygon-website-tag" not in script


def test_shared_runner_setup_is_offline_and_has_a_cleanup_margin() -> None:
    env = ENV_SCRIPT.read_text()

    assert "grid5000_run_setup() {" in env
    assert 'bundle_dir="${GRID5000_BUNDLE_DIR:-$job_dir/bundle}"' in env
    assert "GRID5000_TIME_BUDGET_SECONDS:-1500" in env
    assert "GRID5000_BATCH_ROWS:-256" in env
    assert "export HF_HUB_OFFLINE=1" in env
    assert "export TRANSFORMERS_OFFLINE=1" in env
    assert "export UV_NO_DEV=1" in env
    assert 'run_arguments+=(--job-id "$OAR_JOB_ID")' in env
    for name in ("run_language_detection.sh", "run_sentence_segmentation.sh"):
        script = (SCRIPT_ROOT / name).read_text()
        assert "grid5000_run_setup" in script, name
        assert "HF_HUB_OFFLINE" not in script, name
        assert "--batch-rows" not in script, name


def _env_locator(script: str) -> str:
    start = script.index('env_script="$(dirname')
    end = script.index('source "$env_script"')
    return script[start:end]


def test_env_locator_snippet_is_identical_in_every_node_job_script() -> None:
    locators = {_env_locator((SCRIPT_ROOT / name).read_text()) for name in NODE_JOB_SCRIPTS}

    assert len(locators) == 1


def test_bootstrap_stage_wrappers_differ_only_in_the_stage_argument() -> None:
    language = (SCRIPT_ROOT / "bootstrap_language_runtime.sh").read_text()
    sentences = (SCRIPT_ROOT / "bootstrap_sentence_runtime.sh").read_text()

    assert language.endswith('exec bash "$shared" language\n')
    assert sentences.endswith('exec bash "$shared" sentences\n')
    assert language.removesuffix("language\n") == sentences.removesuffix("sentences\n")


def test_runtime_bootstrap_is_locked_and_runtime_only() -> None:
    script = (SCRIPT_ROOT / "bootstrap_runtime.sh").read_text()

    assert "#OAR -l host=1/gpu=1,walltime=0:30" in script
    assert "uv sync --locked --no-dev" in script
    assert "--python 3.12" in script
    assert "language) extra=() ;;" in script
    assert "--offline" not in script


def test_reserved_node_sentence_runner_is_offline_and_has_a_cleanup_margin() -> None:
    script = (SCRIPT_ROOT / "run_sentence_segmentation.sh").read_text()

    assert "#OAR -l host=1/gpu=1,walltime=0:30" in script
    assert '"${run_arguments[@]}"' in script
    assert "--offline" in script
    assert "--extra sentences" in script
    assert 'LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu' in script
    assert 'grid5000_run_setup --device "${GRID5000_DEVICE:-cuda}"' in script
    assert "python -m osm_polygon_website_tag.application.grid5000_sentence_runner" in script
    assert "osm-polygon-website-tag" not in script


def test_sentence_transfer_wrappers_stay_on_the_frontend_boundary() -> None:
    prepare = (SCRIPT_ROOT / "prepare_sentence_segmentation.sh").read_text()
    sync = (SCRIPT_ROOT / "sync_bundle.sh").read_text()

    assert "grid5000-prepare-sentences" in prepare
    assert "--model-dir" in prepare
    assert "--model-revision" in prepare
    assert "OSM_POLY_MAX_ROWS" in prepare
    assert "grid5000-sync-sentences" in sync
    for script in (prepare, sync):
        assert "oarsub" not in script
        assert "python -m" not in script


def test_sentence_runtime_bootstrap_installs_only_the_locked_segmentation_extra() -> None:
    script = (SCRIPT_ROOT / "bootstrap_runtime.sh").read_text()

    assert "sentences) extra=(--extra sentences) ;;" in script
    assert 'uv sync --locked --no-dev ${extra[@]+"${extra[@]}"} --python 3.12' in script
    assert "--offline" not in script


def test_grid5000_scripts_do_not_contain_credentials() -> None:
    scripts = "\n".join(
        (SCRIPT_ROOT / name).read_text() for name in (*SCRIPT_NAMES, ENV_SCRIPT.name)
    )

    assert "HF_TOKEN" not in scripts
    assert "HUGGING_FACE_HUB_TOKEN" not in scripts


def test_shell_and_docs_defaults_match_the_python_grid_constants() -> None:
    env = ENV_SCRIPT.read_text()
    readme = (SCRIPT_ROOT / "README.md").read_text()

    assert DEFAULT_GRID_LANGUAGE_BATCH_ROWS == DEFAULT_GRID_SENTENCE_BATCH_ROWS == 256
    assert f"GRID5000_BATCH_ROWS:-{DEFAULT_GRID_LANGUAGE_BATCH_ROWS}}}" in env
    assert f"GRID5000_TIME_BUDGET_SECONDS:-{DEFAULT_GRID_TIME_BUDGET_SECONDS}}}" in env
    assert f"at most {DEFAULT_GRID_LANGUAGE_BATCH_ROWS} rows per detector batch" in readme
