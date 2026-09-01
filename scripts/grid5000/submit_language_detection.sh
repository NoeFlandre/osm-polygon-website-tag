#!/usr/bin/env bash
set -euo pipefail

job_dir="${GRID5000_JOB_DIR:?set GRID5000_JOB_DIR to the staged job directory}"
repo_dir="${GRID5000_REPO_DIR:-$job_dir/checkout}"
default_job_script="$job_dir/run_language_detection.sh"
if [[ ! -f "$default_job_script" ]]; then
  default_job_script="$repo_dir/scripts/grid5000/run_language_detection.sh"
fi
job_script="${GRID5000_JOB_SCRIPT:-$default_job_script}"
queue="${GRID5000_QUEUE:-abaca}"
gpus="${GRID5000_GPUS:-1}"
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

if [[ ! "$gpus" =~ ^[1-9][0-9]*$ ]]; then
  printf 'GRID5000_GPUS must be a positive integer: %s\n' "$gpus" >&2
  exit 1
fi

before_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
usagepolicycheck -t > "$policy_log_dir/usagepolicy-before-$before_stamp.txt"

submission="$(oarsub -q "$queue" -l "host=1/gpu=$gpus,walltime=0:30" -S "$job_script")"
if [[ ! "$submission" =~ ([0-9]+) ]]; then
  printf 'Could not identify the OAR job ID from submission output.\n%s\n' "$submission" >&2
  exit 1
fi
job_id="${BASH_REMATCH[1]}"
printf '%s\n' "$job_id" > "$active_marker"

after_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
usagepolicycheck -t > "$policy_log_dir/usagepolicy-after-$after_stamp.txt"
printf '%s\n' "$job_id"
