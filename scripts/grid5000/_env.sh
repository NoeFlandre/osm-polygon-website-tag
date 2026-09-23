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
