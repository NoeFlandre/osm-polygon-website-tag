# shellcheck shell=bash
# Shared reserved-node environment, sourced (never executed or submitted) by
# the Grid'5000 job scripts. Bump the module versions here and only here.
#
# Sets job_dir, repo_dir and uv_cache_dir, loads the pinned modules, exports
# UV_CACHE_DIR, and leaves the working directory at the repository checkout.

if [[ -f /etc/profile.d/modules.sh ]]; then
  # shellcheck source=/dev/null
  source /etc/profile.d/modules.sh
fi
module load python/3.12.12 uv/0.10.12 expat/2.7.1

job_dir="${GRID5000_JOB_DIR:-$PWD}"
repo_dir="${GRID5000_REPO_DIR:-$job_dir/checkout}"
uv_cache_dir="${GRID5000_UV_CACHE_DIR:-$job_dir/uv-cache}"

cd "$repo_dir"
export UV_CACHE_DIR="$uv_cache_dir"

# Shared reserved-node runner setup for run_language_detection.sh and
# run_sentence_segmentation.sh. Sets bundle_dir, time_budget_seconds and
# batch_rows, exports the offline environment, and fills run_arguments with
# the common runner flags. Stage-specific flags passed as arguments are
# appended before --job-id.
grid5000_run_setup() {
  bundle_dir="${GRID5000_BUNDLE_DIR:-$job_dir/bundle}"
  time_budget_seconds="${GRID5000_TIME_BUDGET_SECONDS:-1500}"
  batch_rows="${GRID5000_BATCH_ROWS:-256}"

  export HF_HUB_OFFLINE=1
  export TRANSFORMERS_OFFLINE=1
  export UV_NO_DEV=1

  run_arguments=(
    --bundle-dir "$bundle_dir"
    --time-budget-seconds "$time_budget_seconds"
    --batch-rows "$batch_rows"
    "$@"
  )
  if [[ -n "${OAR_JOB_ID:-}" ]]; then
    run_arguments+=(--job-id "$OAR_JOB_ID")
  fi
}
