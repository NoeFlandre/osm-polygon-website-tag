#!/usr/bin/env bash
#OAR -l host=1/gpu=1,walltime=0:30
#OAR -O OAR_%jobid%.out
#OAR -E OAR_%jobid%.err
set -euo pipefail

# Submitted by name with `oarsub -S`, which passes no arguments, so this
# stage-specific entry point stays; the work lives in bootstrap_runtime.sh.
shared="$(dirname "${BASH_SOURCE[0]}")/bootstrap_runtime.sh"
if [[ ! -f "$shared" ]]; then
  shared="${GRID5000_REPO_DIR:-${GRID5000_JOB_DIR:-$PWD}/checkout}/scripts/grid5000/bootstrap_runtime.sh"
fi
exec bash "$shared" sentences
