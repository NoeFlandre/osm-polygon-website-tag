# Data and remotes

This project keeps code, immutable inputs, local run artifacts, and public publication separate. A local artifact is not public until an apply-mode upload sends it. A complete snapshot also needs the final receipt verification.

## Local paths

The code and the tests stay in the Git checkout. The production PBFs are in a read-only directory that you choose. Pass it with `--source-root`. The generated data goes under the data root. The default data root is `./data`. To put it in a different place, set `OSM_POLY_DATA_DIR` (in the environment or in `.env`). The `--output-root` of the CLI is explicit for each run. It must stay outside the source root. The CLI still accepts the previous `…-data` root when you address an existing run explicitly. New output uses the canonical project root.

## Immutable source boundary

`--source-root` is a read-only input. The safety module refuses an output path that is equal to the source root, or inside it. The pipeline records the filename, the size, and the nanosecond mtime of each PBF. It checks these values before and after processing. It never copies, renames, moves, hashes, or modifies the source file.

## Website crawl policy

Before each page request, the fetcher reads the `robots.txt` of that origin and caches it. It uses the same checks for public IP, redirect, timeout, and response size as for page requests. The fetcher records a disallowed URL as `robots_disallowed`. This status adds to the failure counts of the card. The crawler obeys a published `Crawl-delay`, in addition to its configured limit for each host. A missing policy, or a 404/410 response, permits fetching. The crawler treats malformed policy text as a policy with no rules. An unsafe redirect or a failure of the robots server prevents the page request.

This policy governs the network requests of the updated crawler. The existing successful text in local caches or frozen snapshots stays. The crawler does not check it again later, and does not remove it.

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

`stats.json` is the machine-readable report of the geometry and of the global text population. It is described in [Polygon geometry statistics](#polygon-geometry-statistics).

These files are local run artifacts. The staging data, the DuckDB spill data, the URL-cache files, and the enrichment checkpoint parts support the resume. They are not part of the publication plan. `uploaded_polygons.json` is operational state. The completion receipt is the final allow-list. It records the path, the size, and the SHA-256 of each publishable file.

## GitHub remote

The source repository is [NoeFlandre/osm-polygon-website-tag](https://github.com/NoeFlandre/osm-polygon-website-tag). On `main`, the Pages workflow builds `docs/` with `mkdocs build --strict`. It deploys through GitHub Actions. Source changes and generated dataset artifacts have separate lifecycles. The production run does not write into Git.

## Hugging Face dataset remote

The public dataset repository is [NoeFlandre/osm-polygon-website-tag](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag). Use the CLI. Do not upload a run directory by hand:

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

`publish` is a dry run, unless `--apply` is present. It runs the verification again before any upload. It refuses a partial run or a tampered run. The credentials come from `HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`, or the local Hugging Face credential store. The CLI never accepts a token flag.

For a reviewed end-to-end run, `run-all --apply` uploads an enriched polygon shard and a refreshed card and map bundle. Then it records the acknowledgement and continues. When you repeat the command, it resumes the successful URL results, the durable enrichment batches, and the acknowledged source uploads. Before new uploads, it reconciles the local upload checkpoint with the remote polygon SHA-256 values. It fails closed on a malformed or mismatched checkpoint.

When the full inventory is enriched, the command finalizes the analysis, the card, the manifests, and the completion receipt together. The final upload uses only the receipt-bound allow-list. So a local analysis file is not a public analysis file until the final apply upload succeeds. To see the contents of a published snapshot, inspect the remote tree or the card.

## Existing runs and schema migration

The command projects public schema v1.2 shards locally to v1.3. It reads no PBF and fetches no website. Only the changed content and its regenerated card need a later upload. For a run that you created before the map contract, refresh the local bundle:

```bash
uv run --locked osm-polygon-website-tag refresh-card \
  --run-dir '<output-root>/<run-id>'
```

This migration is local-only. It rebuilds the map, the README, the YAML, and the receipt from the existing Parquets. It makes no remote call.

## Why code and data are separate

- Git stays cloneable and reviewable, because planet-scale PBFs and generated Parquets stay outside the repository.
- A dedicated data volume gives the capacity that the immutable inputs and the resumable runs need.
- Each extraction is a new directory that the run owns. So the raw inputs stay stable. You can inspect or resume a failed run, and the source stays unchanged.

The derived dataset carries the ODbL 1.0 notice, the OpenStreetMap contributor attribution, and the Geofabrik extract-provider attribution. They are in its generated card and in `dataset.yaml`.

## Polygon geometry statistics

`build-card` writes `stats.json` next to `README.md`. The geometry fields come from each row of the selected `polygons/*.parquet` shards. The text fields come from the same shards. The reducer keeps one deterministic canonical winner for each global `(osm_type, osm_id)`. Both populations use no sampling, no truncation, no external lookup, and no recomputation from the raw PBF. The geometry pass reads only the columns `area_m2`, `bbox`, `geometry`, and `osm_primary_tag`. It reads one record batch at a time. The text reducer uses bounded DuckDB spill storage. The command `osm-polygon-website-tag geometry-stats --run-dir <run>` prints the same document. It writes nothing.

A regeneration from unchanged artifacts is byte-identical. The command does not rewrite the existing file. The verification computes the report again. It rejects a missing or stale `stats.json`, in the same way as it does for `README.md` and `dataset.yaml`. The receipt binds the file, and the file is published.

The fields below use the WGS84 ellipsoid for all computations:

| Field | Meaning |
| --- | --- |
| `schema_version` | Contract version of this document (`v2`). |
| `population_scope` | The geometry population that the report covers: `published polygon rows`. |
| `text_population_scope` | The text and map population: global unique `(osm_type, osm_id)` identities with successful non-empty `website` or `contact:website` text. |
| `text_population` | Canonical global counts, words, URLs, status buckets, and language totals. The card and the map share them. Its `unique_identity_count` is the map and card population. |
| `row_count` | Number of public polygon rows that the report covers. |
| `area.summary` | `area_m2` distribution: `row_count`, `total`, `minimum`, `maximum`, `mean`, `median`, and the `percentiles` `p1`, `p5`, `p25`, `p50`, `p75`, `p95`, `p99`. |
| `area.histogram` | Stable log-scale buckets. All thirteen buckets are always present, in this order: `0`, `<1e0`, `1e0-1e1` … `1e9-1e10`, `>=1e10`. |
| `area.zero_area_row_count` | Rows with an area of exactly zero (degenerate). |
| `area.below_one_m2_row_count` | Rows below one square metre (suspiciously small). |
| `shape.polygon_row_count` / `shape.multipolygon_row_count` | Rows by the stored GeoJSON geometry type. |
| `shape.with_holes_row_count` | Rows with at least one inner ring. |
| `shape.hole_ring_count` | Total number of inner rings in all rows. |
| `shape.vertices_per_row` | Distribution of the stored coordinate pairs for each row. |
| `shape.rings_per_row` | Distribution of the rings (outer and inner) for each row. |
| `shape.components_per_multipolygon` | Distribution of the components, for MultiPolygon rows only. |
| `extent.bbox` | Dataset bounding box `[min_lon, min_lat, max_lon, max_lat]`. It is `null` when no row is covered. |
| `extent.width_degrees` / `extent.height_degrees` | Bounding-box span of each row in decimal degrees. |
| `extent.width_m` / `extent.height_m` | Geodesic span in metres. Width: across the box at its mid-latitude. Height: along its mid-longitude meridian. |
| `extent.antimeridian_row_count` | Rows with a bounding box that spans more than 180° of longitude. Extraction rejects antimeridian crossings. So this value is zero for a current snapshot. |
| `extent.polar_row_count` | Rows with a bounding box that reaches 85° of latitude or more. |
| `per_source` | One entry for each source PBF, in sorted shard order, with `row_count` and the same `area_m2` summary. |
| `per_osm_primary_tag` | One entry for each `osm_primary_tag`, with the most rows first, with `row_count` and `total_area_m2`. |

Each distribution summary is exact. The totals use the order-independent `math.fsum`. The percentiles are exact nearest-rank values over each row. Each reported float is rounded to six decimals. An empty selection reports zeroed summaries and a `null` bounding box.
