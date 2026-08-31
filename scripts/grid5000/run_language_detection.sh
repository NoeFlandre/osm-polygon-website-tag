#!/usr/bin/env bash
#OAR -l host=1/core=2,walltime=0:30
#OAR -O OAR_%jobid%.out
#OAR -E OAR_%jobid%.err
set -euo pipefail

job_dir="${GRID5000_JOB_DIR:-$PWD}"
repo_dir="${GRID5000_REPO_DIR:-$job_dir/checkout}"
bundle_dir="${GRID5000_BUNDLE_DIR:-$job_dir/bundle}"
time_budget_seconds="${GRID5000_TIME_BUDGET_SECONDS:-1500}"
batch_rows="${GRID5000_BATCH_ROWS:-512}"
uv_cache_dir="${GRID5000_UV_CACHE_DIR:-$job_dir/uv-cache}"

cd "$repo_dir"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export UV_CACHE_DIR="$uv_cache_dir"

arguments=(
  grid5000-run
  --bundle-dir "$bundle_dir"
  --time-budget-seconds "$time_budget_seconds"
  --batch-rows "$batch_rows"
)
if [[ -n "${OAR_JOB_ID:-}" ]]; then
  arguments+=(--job-id "$OAR_JOB_ID")
fi

exec uv run --locked --offline osm-polygon-website-tag "${arguments[@]}"
