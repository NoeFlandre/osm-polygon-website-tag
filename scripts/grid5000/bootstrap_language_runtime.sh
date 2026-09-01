#!/usr/bin/env bash
#OAR -l host=1/gpu=1,walltime=0:30
#OAR -O OAR_%jobid%.out
#OAR -E OAR_%jobid%.err
set -euo pipefail

if ! command -v module >/dev/null 2>&1 && [[ -f /etc/profile.d/modules.sh ]]; then
  # shellcheck source=/dev/null
  source /etc/profile.d/modules.sh
fi
module load python/3.12.12 uv/0.10.12

job_dir="${GRID5000_JOB_DIR:-$PWD}"
repo_dir="${GRID5000_REPO_DIR:-$job_dir/checkout}"
uv_cache_dir="${GRID5000_UV_CACHE_DIR:-$job_dir/uv-cache}"

cd "$repo_dir"
export UV_CACHE_DIR="$uv_cache_dir"

exec uv sync --locked --no-dev --python 3.12
