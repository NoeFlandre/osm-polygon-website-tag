# Data and remotes

This project separates code, immutable inputs, local run artifacts, and public
publication. A local artifact is not public until an apply-mode upload sends
it; a complete snapshot additionally requires the final receipt verification.

## Local paths

Code and tests stay in the Git checkout. The reviewed production PBFs are
under:

```text
/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw
```

Generated data defaults to:

```text
/Volumes/Seagate M3/projects/osm-polygon-website-tag
```

Set `OSM_POLY_DATA_DIR` to override that generated-data root. The CLI's
`--output-root` is explicit for each run and must remain outside the source
root. The previous `…-data` root remains accepted when an existing run is
addressed explicitly, but new output uses the canonical project root.

## Immutable source boundary

`--source-root` is read-only input. The safety module refuses an output path
that is equal to or contained by it. The pipeline records each PBF's filename,
size, and nanosecond mtime, checks those values before and after processing,
and never copies, renames, moves, hashes, or modifies the source file.

## Run artifacts

Each run owns a directory under the output root:

```text
<output-root>/<run-id>/
  polygons/<source-stem>.parquet
  analysis_observations/<source-stem>.parquet
  rejections/<source-stem>.parquet
  analysis/*.parquet
  manifests/
    run.json
    expected_sources.json
    sources.json
    uploaded_polygons.json
    completion_receipt.json
  assets/geographic_polygon_density.png
  README.md
  dataset.yaml
  stats.json
```

`stats.json` is the machine-readable polygon geometry report described in
[Polygon geometry statistics](#polygon-geometry-statistics).

These files are local run artifacts. Staging, DuckDB spill data, URL-cache
files, and enrichment checkpoint parts support resume and are not part of the
publication plan. `uploaded_polygons.json` is operational state; the
completion receipt is the final allow-list and records each publishable path,
size, and SHA-256.

## GitHub remote

The source repository is
[NoeFlandre/osm-polygon-website-tag](https://github.com/NoeFlandre/osm-polygon-website-tag).
The Pages workflow builds `docs/` with `mkdocs build --strict` on `main` and
deploys through GitHub Actions. Source changes and generated dataset artifacts
have separate lifecycles: the production run does not write into Git.

## Hugging Face dataset remote

The public dataset repository is
[NoeFlandre/osm-polygon-website-tag](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag).
Use the CLI rather than uploading a run directory manually:

```bash
# Read-only checks and a publication plan.
uv run --locked osm-polygon-website-tag verify-results --run-dir '<run-dir>'
uv run --locked osm-polygon-website-tag publish-plan --run-dir '<run-dir>'
uv run --locked osm-polygon-website-tag publish --run-dir '<run-dir>'

# After separate review and approval.
hf auth login
uv run --locked osm-polygon-website-tag publish \
  --run-dir '<run-dir>' \
  --repo-id 'NoeFlandre/osm-polygon-website-tag' \
  --apply
```

`publish` is a dry run unless `--apply` is present. It re-runs verification
before any upload and refuses a partial or tampered run. Credentials come from
`HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`, or the local Hugging Face credential
store; the CLI never accepts a token flag.

For a reviewed end-to-end run, `run-all --apply` uploads an enriched polygon
shard and refreshed card/map bundle, then records its acknowledgement before
continuing. Repeating the command resumes successful URL results, durable
enrichment batches, and acknowledged source uploads. It reconciles the local
upload checkpoint with remote polygon SHA-256 values before new uploads; a
malformed or mismatched checkpoint fails closed.

When the full inventory is enriched, analysis, card, manifests, and the
completion receipt are finalized together. The final upload uses only the
receipt-bound allow-list. Therefore a local analysis file is not a public
analysis file until the final apply upload succeeds; inspect the remote tree or
card for the contents of a particular published snapshot.

## Existing runs and schema migration

Public schema v1.2 shards are projected locally to v1.3 without PBF reads or
website fetches. Only changed content and its regenerated card need a later
upload. For a run created before the map contract, refresh the local bundle:

```bash
uv run --locked osm-polygon-website-tag refresh-card \
  --run-dir '<output-root>/<run-id>'
```

This migration is local-only: it rebuilds the map, README, YAML, and receipt
from existing Parquets and performs no remote call.

## Why separate code and data

- Git remains cloneable and reviewable because planet-scale PBFs and generated
  Parquets stay off-repository.
- The Seagate volume provides the capacity needed for immutable inputs and
  resumable runs.
- Every extraction is a new run-owned directory, so raw inputs remain stable
  and a failed run can be inspected or resumed without rewriting its source.

The derived dataset carries the ODbL 1.0 notice, OpenStreetMap contributor
attribution, and Geofabrik extract-provider attribution in its generated card
and `dataset.yaml`.

## Polygon geometry statistics

`build-card` writes `stats.json` next to `README.md`, from the same single
pass over the validated public shards that produces the card's
**Polygon geometry** block. Every value covers every row of the selected
`polygons/*.parquet` shards -- no sampling, no truncation, no external lookup,
and no recomputation from the raw PBFs. Only the `area_m2`, `bbox`,
`geometry`, and `osm_primary_tag` columns are read, one record batch at a
time. `osm-polygon-website-tag geometry-stats --run-dir <run>` prints the same
document without writing anything.

Regeneration from unchanged artifacts is byte-identical, and the existing
file is left untouched rather than rewritten. Verification recomputes the
report and rejects a missing or stale `stats.json`, exactly as it does for
`README.md` and `dataset.yaml`. The file is receipt-bound and published.

Fields, all computed on the WGS84 ellipsoid:

| Field | Meaning |
| --- | --- |
| `schema_version` | Contract version of this document (`v1`). |
| `population_scope` | The geometry population covered by the report: `published polygon rows`. |
| `row_count` | Public polygon rows covered. |
| `area.summary` | `area_m2` distribution: `row_count`, `total`, `minimum`, `maximum`, `mean`, `median`, and `percentiles` `p1`, `p5`, `p25`, `p50`, `p75`, `p95`, `p99`. |
| `area.histogram` | Stable log-scale buckets, always all thirteen in order: `0`, `<1e0`, `1e0-1e1` … `1e9-1e10`, `>=1e10`. |
| `area.zero_area_row_count` | Rows whose area is exactly zero (degenerate). |
| `area.below_one_m2_row_count` | Rows below one square metre (suspiciously small). |
| `shape.polygon_row_count` / `shape.multipolygon_row_count` | Rows by stored GeoJSON geometry type. |
| `shape.with_holes_row_count` | Rows carrying at least one inner ring. |
| `shape.hole_ring_count` | Total inner rings across every row. |
| `shape.vertices_per_row` | Distribution of stored coordinate pairs per row. |
| `shape.rings_per_row` | Distribution of rings (outer and inner) per row. |
| `shape.components_per_multipolygon` | Distribution of components, over MultiPolygon rows only. |
| `extent.bbox` | Dataset bounding box `[min_lon, min_lat, max_lon, max_lat]`, `null` when no row was covered. |
| `extent.width_degrees` / `extent.height_degrees` | Per-row bounding-box span in decimal degrees. |
| `extent.width_m` / `extent.height_m` | Geodesic span in metres: width across the box at its mid-latitude, height along its mid-longitude meridian. |
| `extent.antimeridian_row_count` | Rows whose bounding box spans more than 180° of longitude. Extraction rejects antimeridian crossings, so this is zero for a current snapshot. |
| `extent.polar_row_count` | Rows whose bounding box reaches 85° of latitude or beyond. |
| `per_source` | One entry per source PBF, in sorted shard order, with `row_count` and the same `area_m2` summary. |
| `per_osm_primary_tag` | One entry per `osm_primary_tag`, most rows first, with `row_count` and `total_area_m2`. |

Every distribution summary is exact: totals use order-independent
`math.fsum`, percentiles are exact nearest-rank values over every row, and
each reported float is rounded to six decimals. Empty selections report zeroed
summaries and a `null` bounding box.
