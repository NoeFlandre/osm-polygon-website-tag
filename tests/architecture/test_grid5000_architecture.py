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
    "prepare_sentence_segmentation.sh",
    "run_sentence_segmentation.sh",
    "sync_sentence_segmentation.sh",
    "bootstrap_sentence_runtime.sh",
)


def test_grid5000_scripts_are_executable() -> None:
    for name in SCRIPT_NAMES:
        path = SCRIPT_ROOT / name
        assert path.is_file()
        assert os.access(path, os.X_OK)


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
    assert "module load python/3.12.12 uv/0.10.12 expat/2.7.1" in script
    assert "if [[ -f /etc/profile.d/modules.sh ]]; then" in script
    assert "if ! command -v module" not in script
    assert "GRID5000_TIME_BUDGET_SECONDS:-1500" in script
    assert "GRID5000_BATCH_ROWS:-256" in script
    assert "--offline" in script
    assert "HF_HUB_OFFLINE=1" in script
    assert "UV_NO_DEV=1" in script
    assert "python -m osm_polygon_website_tag.application.grid5000_runner" in script
    assert "osm-polygon-website-tag" not in script


def test_runtime_bootstrap_is_locked_and_runtime_only() -> None:
    script = (SCRIPT_ROOT / "bootstrap_language_runtime.sh").read_text()

    assert "#OAR -l host=1/gpu=1,walltime=0:30" in script
    assert "module load python/3.12.12 uv/0.10.12 expat/2.7.1" in script
    assert "if [[ -f /etc/profile.d/modules.sh ]]; then" in script
    assert "if ! command -v module" not in script
    assert "uv sync --locked --no-dev --python 3.12" in script
    assert "--offline" not in script


def test_reserved_node_sentence_runner_is_offline_and_has_a_cleanup_margin() -> None:
    script = (SCRIPT_ROOT / "run_sentence_segmentation.sh").read_text()

    assert "#OAR -l host=1/gpu=1,walltime=0:30" in script
    assert "module load python/3.12.12 uv/0.10.12 expat/2.7.1" in script
    assert "if [[ -f /etc/profile.d/modules.sh ]]; then" in script
    assert "GRID5000_TIME_BUDGET_SECONDS:-1500" in script
    assert "GRID5000_BATCH_ROWS:-256" in script
    assert "--offline" in script
    assert "HF_HUB_OFFLINE=1" in script
    assert "TRANSFORMERS_OFFLINE=1" in script
    assert "UV_NO_DEV=1" in script
    assert "--extra sentences" in script
    assert 'LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu' in script
    assert '--device "${GRID5000_DEVICE:-cuda}"' in script
    assert "python -m osm_polygon_website_tag.application.grid5000_sentence_runner" in script
    assert "osm-polygon-website-tag" not in script


def test_sentence_transfer_wrappers_stay_on_the_frontend_boundary() -> None:
    prepare = (SCRIPT_ROOT / "prepare_sentence_segmentation.sh").read_text()
    sync = (SCRIPT_ROOT / "sync_sentence_segmentation.sh").read_text()

    assert "grid5000-prepare-sentences" in prepare
    assert "--model-dir" in prepare
    assert "--model-revision" in prepare
    assert "OSM_POLY_MAX_ROWS" in prepare
    assert "grid5000-sync-sentences" in sync
    for script in (prepare, sync):
        assert "oarsub" not in script
        assert "python -m" not in script


def test_sentence_runtime_bootstrap_installs_only_the_locked_segmentation_extra() -> None:
    script = (SCRIPT_ROOT / "bootstrap_sentence_runtime.sh").read_text()

    assert "#OAR -l host=1/gpu=1,walltime=0:30" in script
    assert "module load python/3.12.12 uv/0.10.12 expat/2.7.1" in script
    assert "uv sync --locked --no-dev --extra sentences --python 3.12" in script
    assert "--offline" not in script


def test_grid5000_scripts_do_not_contain_credentials() -> None:
    scripts = "\n".join((SCRIPT_ROOT / name).read_text() for name in SCRIPT_NAMES)

    assert "HF_TOKEN" not in scripts
    assert "HUGGING_FACE_HUB_TOKEN" not in scripts
