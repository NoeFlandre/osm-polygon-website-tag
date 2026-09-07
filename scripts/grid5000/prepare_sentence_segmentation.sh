#!/usr/bin/env bash
set -euo pipefail

: "${OSM_POLY_RUN_DIR:?set OSM_POLY_RUN_DIR to the Seagate run directory}"
: "${OSM_POLY_BUNDLE_DIR:?set OSM_POLY_BUNDLE_DIR to a new Seagate bundle directory}"
: "${OSM_POLY_MODEL_DIR:?set OSM_POLY_MODEL_DIR to the staged Seagate SaT directory}"
: "${OSM_POLY_MODEL_REVISION:?set OSM_POLY_MODEL_REVISION to the pinned SaT revision}"
: "${OSM_POLY_COMMIT:?set OSM_POLY_COMMIT to the checked-out repository commit}"

arguments=(
  grid5000-prepare-sentences
  --run-dir "$OSM_POLY_RUN_DIR"
  --bundle-dir "$OSM_POLY_BUNDLE_DIR"
  --model-dir "$OSM_POLY_MODEL_DIR"
  --model-revision "$OSM_POLY_MODEL_REVISION"
  --commit "$OSM_POLY_COMMIT"
)

if [[ -n "${OSM_POLY_TIME_BUDGET_SECONDS:-}" ]]; then
  arguments+=(--time-budget-seconds "$OSM_POLY_TIME_BUDGET_SECONDS")
fi
if [[ -n "${OSM_POLY_BATCH_ROWS:-}" ]]; then
  arguments+=(--batch-rows "$OSM_POLY_BATCH_ROWS")
fi
if [[ -n "${OSM_POLY_MAX_ROWS:-}" ]]; then
  arguments+=(--max-rows "$OSM_POLY_MAX_ROWS")
fi

exec uv run --locked osm-polygon-website-tag "${arguments[@]}"
