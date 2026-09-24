#!/usr/bin/env bash
set -euo pipefail

# usage: sync_bundle.sh <language|sentences>
stage="${1:?usage: sync_bundle.sh <language|sentences>}"
case "$stage" in
  language) command=grid5000-sync ;;
  sentences) command=grid5000-sync-sentences ;;
  *)
    printf 'Unknown stage: %s (expected language or sentences)\n' "$stage" >&2
    exit 2
    ;;
esac

: "${OSM_POLY_RUN_DIR:?set OSM_POLY_RUN_DIR to the Seagate run directory}"
: "${OSM_POLY_BUNDLE_DIR:?set OSM_POLY_BUNDLE_DIR to the returned Seagate bundle directory}"

exec uv run --locked osm-polygon-website-tag "$command" \
  --bundle-dir "$OSM_POLY_BUNDLE_DIR" \
  --run-dir "$OSM_POLY_RUN_DIR"
