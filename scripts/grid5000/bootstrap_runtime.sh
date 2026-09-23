#!/usr/bin/env bash
#OAR -l host=1/gpu=1,walltime=0:30
#OAR -O OAR_%jobid%.out
#OAR -E OAR_%jobid%.err
set -euo pipefail

# usage: bootstrap_runtime.sh <language|sentences>
# Installs the locked runtime (no dev group); `sentences` adds the
# segmentation extra.
stage="${1:?usage: bootstrap_runtime.sh <language|sentences>}"
case "$stage" in
  language) extra=() ;;
  sentences) extra=(--extra sentences) ;;
  *)
    printf 'Unknown stage: %s (expected language or sentences)\n' "$stage" >&2
    exit 2
    ;;
esac

# A job script may run as a copy staged in the job directory, so fall back
# to the checkout's helper when none sits beside it.
env_script="$(dirname "${BASH_SOURCE[0]}")/_env.sh"
if [[ ! -f "$env_script" ]]; then
  env_script="${GRID5000_REPO_DIR:-${GRID5000_JOB_DIR:-$PWD}/checkout}/scripts/grid5000/_env.sh"
fi
# shellcheck source=scripts/grid5000/_env.sh
source "$env_script"

exec uv sync --locked --no-dev ${extra[@]+"${extra[@]}"} --python 3.12
