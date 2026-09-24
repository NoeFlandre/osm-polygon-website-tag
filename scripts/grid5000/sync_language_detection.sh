#!/usr/bin/env bash
set -euo pipefail

exec "$(dirname "${BASH_SOURCE[0]}")/sync_bundle.sh" language "$@"
