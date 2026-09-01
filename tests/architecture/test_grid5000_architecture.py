"""Architecture checks for the policy-aware Grid'5000 shell boundary."""

from __future__ import annotations

import os
from pathlib import Path

SCRIPT_ROOT = Path("scripts/grid5000")
SCRIPT_NAMES = (
    "bootstrap_language_runtime.sh",
    "prepare_language_detection.sh",
    "run_language_detection.sh",
    "submit_language_detection.sh",
    "sync_language_detection.sh",
)


def test_grid5000_scripts_are_executable() -> None:
    for name in SCRIPT_NAMES:
        path = SCRIPT_ROOT / name
        assert path.is_file()
        assert os.access(path, os.X_OK)


def test_submit_script_checks_policy_around_submission() -> None:
    script = (SCRIPT_ROOT / "submit_language_detection.sh").read_text()
    submit_call = "oarsub -q"

    assert script.count("usagepolicycheck -t") >= 2
    assert script.index("usagepolicycheck -t") < script.index(submit_call)
    assert script.rindex("usagepolicycheck -t") > script.index(submit_call)
    assert "walltime=0:30" in script
    assert "host=1/gpu=" in script
    assert "GRID5000_GPUS:-1" in script
    assert "GRID5000_REPO_DIR" in script
    assert "scripts/grid5000/run_language_detection.sh" in script
    assert "active" in script.lower()


def test_reserved_node_runner_is_offline_and_has_a_cleanup_margin() -> None:
    script = (SCRIPT_ROOT / "run_language_detection.sh").read_text()

    assert "#OAR -l host=1/gpu=1/core=2,walltime=0:30" in script
    assert "module load python/3.12.12 uv/0.10.12" in script
    assert "GRID5000_TIME_BUDGET_SECONDS:-1500" in script
    assert "--offline" in script
    assert "HF_HUB_OFFLINE=1" in script
    assert "UV_NO_DEV=1" in script


def test_runtime_bootstrap_is_locked_and_runtime_only() -> None:
    script = (SCRIPT_ROOT / "bootstrap_language_runtime.sh").read_text()

    assert "#OAR -l host=1/gpu=1/core=2,walltime=0:30" in script
    assert "module load python/3.12.12 uv/0.10.12" in script
    assert "uv sync --locked --no-dev --python 3.12" in script
    assert "--offline" not in script


def test_grid5000_scripts_do_not_contain_credentials() -> None:
    scripts = "\n".join((SCRIPT_ROOT / name).read_text() for name in SCRIPT_NAMES)

    assert "HF_TOKEN" not in scripts
    assert "HUGGING_FACE_HUB_TOKEN" not in scripts
