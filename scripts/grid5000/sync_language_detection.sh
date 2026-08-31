#!/usr/bin/env bash
set -euo pipefail

: "${OSM_POLY_RUN_DIR:?set OSM_POLY_RUN_DIR to the Seagate run directory}"
: "${OSM_POLY_BUNDLE_DIR:?set OSM_POLY_BUNDLE_DIR to the returned Seagate bundle directory}"

exec uv run --locked osm-polygon-website-tag grid5000-sync \
  --bundle-dir "$OSM_POLY_BUNDLE_DIR" \
  --run-dir "$OSM_POLY_RUN_DIR"
