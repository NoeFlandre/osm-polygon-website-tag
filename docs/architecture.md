# Architecture

The code lives locally. Production PBFs are immutable read-only inputs under
an explicitly supplied source root; run artifacts are written to a separate
local output root.

## Package boundaries

The source and test trees are organized by responsibility:

- `contracts`: exact Arrow schemas and frozen text contracts;
- `domain`: deterministic OSM classification and geometry rules;
- `storage`: bounded, transactional local persistence;
- `web`: safe HTTP retrieval, Trafilatura extraction, and URL caching;
- `runtime`: configuration, paths, safety, and run lifecycle;
- `pipeline`: extraction, enrichment, and analysis stages;
- `reporting`: artifact-derived cards, verification, and finalization;
- `publishing`: Hugging Face credential and upload adapters;
- `application`: workflow composition and CLI dispatch.

Dependencies flow toward lower-level packages. `application` is the only
composition layer and no lower package imports it. The executable architecture
tests enforce allowed package edges, reject cycles, and ensure every package
documents its boundary.

## Pipeline

1. `init` records the exact source inventory using filename, byte size, and
   nanosecond mtime. It rejects an output root inside the source root.
2. `extract` processes one inventoried PBF. libosmium assembles closed ways
   and polygon relations; bounded Arrow sinks and a SQLite candidate ledger
   produce one public, comparison, and rejection Parquet per source.
3. Before the next PBF is opened, website-text enrichment safely downloads both website tag values,
   extracts full main text with Trafilatura, and transactionally migrates each
   polygon shard to the current public schema. A run-owned SQLite cache reuses successes and
   retries failures on a later invocation.
4. After every enriched PBF, the cumulative card is recomputed from current
   Parquets and uploaded with that shard; the acknowledgement is persisted
   before the next source transaction begins.
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
completed extraction bundles (including bundles produced by old extract-all
runs), migrates legacy shards without reopening PBFs, checkpoints successful
URL text and per-PBF enriched uploads, and performs the receipt-bound full
upload only after final verification.
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

The public schema is versioned in `contracts/polygon_schema.py`; the generated card
renders its column names, Arrow types, nullability, and documentation.
Schema v1.3 stores full Trafilatura text and exact Unicode `\w+` word counts
independently for `website` and `contact:website`.

Card rendering is a pure presentation layer over `CardStats`. Its compact
snapshot and website-text tables, combined word total, and top-ten hostname
tables are regenerated from Parquets on every incremental upload. Detailed
analysis stays in `analysis/*.parquet`; optional Hugging Face task metadata is
omitted because no official task category accurately describes the dataset.

The v1.3 public projection removes `preferred_website`,
`preferred_website_source`, `wikidata`, `wikidata_qid`, `wikidata_class`, and
`area_km2`. The comparison schema retains Wikidata for overlap analysis and
`tags` retains every original tag. Existing v1.2 shards are projected in
bounded batches, atomically promoted, and reuploaded through their changed
content hash without PBF reads or website fetches.

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
