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

# shellcheck disable=SC2119 # no stage-specific runner flags
grid5000_run_setup

exec uv run --locked --offline python -m osm_polygon_website_tag.application.grid5000_runner "${run_arguments[@]}"
