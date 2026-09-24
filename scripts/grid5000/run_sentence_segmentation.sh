#!/usr/bin/env bash
#OAR -l host=1/gpu=1,walltime=0:30
#OAR -O OAR_%jobid%.out
#OAR -E OAR_%jobid%.err
set -euo pipefail

# A job script may run as a copy staged in the job directory, so fall back
# to the checkout's helper when none sits beside it.
env_script="$(dirname "${BASH_SOURCE[0]}")/_env.sh"
if [[ ! -f "$env_script" ]]; then
  env_script="${GRID5000_REPO_DIR:-${GRID5000_JOB_DIR:-$PWD}/checkout}/scripts/grid5000/_env.sh"
fi
# shellcheck source=scripts/grid5000/_env.sh
source "$env_script"

bundle_dir="${GRID5000_BUNDLE_DIR:-$job_dir/bundle}"
time_budget_seconds="${GRID5000_TIME_BUDGET_SECONDS:-1500}"
batch_rows="${GRID5000_BATCH_ROWS:-256}"

# The Guix-provided interpreter does not search the node's system library
# path, so torch cannot load the NVIDIA driver stub and would silently
# segment on CPU at a fraction of the throughput.
export LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export UV_NO_DEV=1

arguments=(
  --bundle-dir "$bundle_dir"
  --time-budget-seconds "$time_budget_seconds"
  --batch-rows "$batch_rows"
  --device "${GRID5000_DEVICE:-cuda}"
)
if [[ -n "${OAR_JOB_ID:-}" ]]; then
  arguments+=(--job-id "$OAR_JOB_ID")
fi

exec uv run --locked --offline --extra sentences python -m osm_polygon_website_tag.application.grid5000_sentence_runner "${arguments[@]}"
