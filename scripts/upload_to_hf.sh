#!/usr/bin/env bash
# Verify a completed run and publish it through the guarded project CLI.

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 RUN_DIR [--apply]" >&2
    exit 2
fi

RUN_DIR="$1"
MODE="${2:-}"

if [[ -n "${MODE}" && "${MODE}" != "--apply" ]]; then
    echo "error: the only supported second argument is --apply" >&2
    exit 1
fi

ARGS=(publish --run-dir "${RUN_DIR}")
if [[ "${MODE}" == "--apply" ]]; then
    ARGS+=(--apply)
fi
uv run osm-polygon-website-tag "${ARGS[@]}"
