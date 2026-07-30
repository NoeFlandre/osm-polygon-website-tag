# Architecture

The code lives locally. Production PBFs are immutable read-only inputs under
an explicitly supplied source root; run artifacts are written to a separate
local output root.

## Pipeline

1. `init` records the exact source inventory using filename, byte size, and
   nanosecond mtime. It rejects an output root inside the source root.
2. `extract` processes one inventoried PBF. libosmium assembles closed ways
   and polygon relations; bounded Arrow sinks and a SQLite candidate ledger
   produce one public, comparison, and rejection Parquet per source.
3. Website-text enrichment safely downloads both website tag values,
   extracts full main text with Trafilatura, and transactionally migrates each
   polygon shard to schema v1.2. A run-owned SQLite cache reuses successes and
   retries failures on a later invocation.
4. After every enriched PBF, the cumulative card is recomputed from current
   Parquets and uploaded with that shard.
5. `analyze-results` uses DuckDB external memory and run-owned spill space.
   Large results go directly to staged Parquets and the complete analysis
   bundle is promoted transactionally.
6. `build-card` recomputes every displayed statistic from Parquet artifacts
   and writes deterministic `README.md` and `dataset.yaml`.
7. `finalize-run` verifies the run, moves it to `complete`, and writes a
   receipt binding every publishable relative path, byte size, and SHA-256.
8. `publish` is network-free by default. `--apply` re-verifies a complete run
   and uses Hugging Face's resumable large-folder uploader with a receipt-based
   allow-list.

`run-all` composes these phases for the full recursively discovered inventory.
It validates the same source fingerprints on every restart, skips exact
completed extraction bundles, migrates legacy shards without reopening PBFs,
checkpoints successful URL text and per-PBF enriched uploads, and performs the
receipt-bound full upload only after final verification.
`KeyboardInterrupt` leaves the run in the resumable `extracting` state.

## Run layout

```text
<output-root>/<run-id>/
  polygons/<source-stem>.parquet
  analysis_observations/<source-stem>.parquet
  rejections/<source-stem>.parquet
  analysis/
    cells_global.parquet
    cells_by_source.parquet
    cells_by_region.parquet
    cells_by_osm_type.parquet
    cells_by_primary_category.parquet
    by_website_class_canonical.parquet
    by_contact_website_class_canonical.parquet
    by_source_overlap.parquet
    by_source_dedup.parquet
    duplicate_observations.parquet
    conflicting_snapshots.parquet
    rejections_by_kind.parquet
    hostnames_exact_website.parquet
    hostnames_exact_contact_website.parquet
    top_hostnames_website.parquet
    top_hostnames_contact_website.parquet
  manifests/
    run.json
    expected_sources.json
    sources.json
    uploaded_polygons.json
    completion_receipt.json
  README.md
  dataset.yaml
```

## Public dataset contract

Only `polygons/*.parquet` is declared as the Hugging Face dataset split. Every
source PBF has exactly one shard, including schema-valid empty shards. A row is
an assembled closed way or supported polygon relation with a non-empty
`website` or `contact:website` tag. Wikidata is optional and comparison-only.

The public schema is versioned in `polygon_schema.py`; the generated card
renders its column names, Arrow types, nullability, and documentation.
Schema v1.2 stores full Trafilatura text and exact Unicode `\w+` word counts
independently for `website` and `contact:website`.

## Boundedness and transactions

- Extraction keeps at most the configured row batch in each Python sink.
- Candidate and area-seen reconciliation lives in a run-owned SQLite file.
- DuckDB has an explicit memory limit, one deterministic worker, and a
  run-owned spill directory.
- Source fingerprints are compared before and after reading.
- Per-source three-shard promotion and whole-analysis promotion restore the
  previous bundle on failure.
- Cleanup targets known temporary files only; non-empty diagnostic directories
  are retained.

## Verification

Verification checks exact source inventory, exact Arrow schemas, independent
counts and hashes for all three shard types (including zeros), row invariants,
the eight-cell arithmetic, required analysis/card files, artifact-derived card
text, and the completion receipt. A complete run with any missing, extra,
corrupt, or modified receipt-bound artifact is rejected.

## Licensing

The source code is Apache-2.0. Derived OpenStreetMap data is published under
ODbL 1.0 with OpenStreetMap contributor and Geofabrik attribution. Hugging Face
metadata uses the supported `odbl` identifier.
