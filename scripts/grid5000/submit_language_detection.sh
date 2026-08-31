#!/usr/bin/env bash
set -euo pipefail

job_dir="${GRID5000_JOB_DIR:?set GRID5000_JOB_DIR to the staged job directory}"
job_script="${GRID5000_JOB_SCRIPT:-$job_dir/run_language_detection.sh}"
queue="${GRID5000_QUEUE:-default}"
cores="${GRID5000_CORES:-2}"
active_marker="${GRID5000_ACTIVE_MARKER:-$job_dir/job.active}"
policy_log_dir="${GRID5000_POLICY_LOG_DIR:-$job_dir/logs}"

if [[ -e "$active_marker" ]]; then
  printf 'Refusing duplicate Grid5000 submission; inspect %s first.\n' "$active_marker" >&2
  exit 1
fi
if [[ ! -f "$job_script" ]]; then
  printf 'Missing Grid5000 job script: %s\n' "$job_script" >&2
  exit 1
fi
command -v usagepolicycheck >/dev/null
command -v oarsub >/dev/null
mkdir -p "$policy_log_dir"

before_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
usagepolicycheck -t > "$policy_log_dir/usagepolicy-before-$before_stamp.txt"

submission="$(oarsub -q "$queue" -l "host=1/core=$cores,walltime=0:30" -S "$job_script")"
if [[ ! "$submission" =~ ([0-9]+) ]]; then
  printf 'Could not identify the OAR job ID from submission output.\n%s\n' "$submission" >&2
  exit 1
fi
job_id="${BASH_REMATCH[1]}"
printf '%s\n' "$job_id" > "$active_marker"

after_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
usagepolicycheck -t > "$policy_log_dir/usagepolicy-after-$after_stamp.txt"
printf '%s\n' "$job_id"
