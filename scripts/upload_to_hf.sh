#!/usr/bin/env bash
# Push the local exports directory to the Hugging Face dataset.
#
# Requires the `hf` CLI: `brew install hf`, then `hf auth login` once.
# Honors HF_DATASET_REPO and OSM_POLY_DATA_DIR from the environment
# (loaded from .env if present).

set -euo pipefail

# Load .env if it exists, so the script works without manual exports.
if [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -a
    source .env
    set +a
fi

HF_DATASET_REPO="${HF_DATASET_REPO:-NoeFlandre/osm-polygon-website-tag}"
DATA_ROOT="${OSM_POLY_DATA_DIR:-/Volumes/Seagate M3/projects/osm-polygon-website-tag}"
EXPORTS_DIR="${DATA_ROOT}/exports"

if [[ ! -d "${EXPORTS_DIR}" ]]; then
    echo "error: exports directory not found at ${EXPORTS_DIR}" >&2
    echo "       set OSM_POLY_DATA_DIR or create the directory first." >&2
    exit 1
fi

echo "Uploading ${EXPORTS_DIR} -> ${HF_DATASET_REPO} (dataset)"
hf upload "${HF_DATASET_REPO}" "${EXPORTS_DIR}" --repo-type=dataset
