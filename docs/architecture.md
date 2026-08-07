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
   produce one public, comparison, and rejection Parquet per source. Pure
   geometry and row construction run in a bounded FIFO worker pool over copied
   GeoJSON payloads (four workers and 32 in-flight areas by default), while
   libosmium, SQLite, and Parquet remain callback-thread-owned. Results are
   emitted in source order, and PBFs remain sequential across the inventory.
3. Before the next PBF is opened, website-text enrichment safely downloads both website tag values,
   extracts full main text with Trafilatura, and transactionally migrates each
   polygon shard to the current public schema. A run-owned SQLite cache reuses successes and
   retries failures on a later invocation. Distinct cache misses in each bounded
   Arrow batch use a bounded I/O worker pool (eight workers by default, with a
   safe cap of 32); cache access and row application stay single-threaded and
   input-ordered. Cache commits and source-bound Parquet
   checkpoint parts are flushed at each completed batch, so a `KeyboardInterrupt`
   preserves the enriched prefix and resumes from the first incomplete batch.
   Resume checks inspect only the two text-status columns in bounded Arrow
   batches: `success` and `absent` are terminal, while null or any other
   status remains retryable.
4. After every enriched PBF, the cumulative H3 resolution-3 polygon-density
   summary is computed once from local public rows with a successful `website`
   or `contact:website` text extraction. The
   deterministic logarithmic `assets/geographic_polygon_density.png`, README,
   and YAML are promoted together and uploaded with the changed shard. An
   atomic schema-v2 acknowledgement is persisted before the next source
   transaction begins.
5. `analyze-results` uses DuckDB external memory and run-owned spill space.
   Large results go directly to staged Parquets and the complete analysis
   bundle is promoted transactionally. Each invocation writes into its own
   unique staging directory under `<run_dir>/staging/` (via
   `tempfile.mkdtemp`) and removes only that directory on success,
   ordinary exceptions, or `BaseException` (including `KeyboardInterrupt`).
   Pre-existing or diagnostic subdirectories under `staging/` are never
   touched, so a failed or interrupted analysis never blocks a later
   retry and the previously published `analysis/*.parquet` bundle stays
   intact.
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
upload only after final verification. Resume ordering puts sources with no
acknowledged upload ahead of retryable acknowledged sources, while keeping
already-complete sources last; deterministic filename order breaks ties.
`KeyboardInterrupt` leaves the run in the resumable `extracting` state.
If a completed run predates the H3 card contract, the next `run-all` invocation
refreshes only its local card/map/receipt bundle before any remote action; the
`refresh-card` command exposes the same migration explicitly.
Run metadata and source manifests are read as UTF-8 JSON and structurally
validated at the resume boundary; duplicate filenames, missing fingerprints,
non-integer fingerprints, and malformed JSON fail closed instead of being
silently collapsed into a partial run state.

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
  assets/
    geographic_polygon_density.png
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
The geographic map counts every text-bearing public polygon centroid exactly
once in H3 resolution 3 and uses a logarithmic absolute-count color scale with
the bundled Natural Earth 1:110m land backdrop and no network fetch. The map, README, and YAML are receipt-bound only after
final run-level verification; the per-PBF upload checkpoint remains operational
state.

The v1.3 public projection removes `preferred_website`,
`preferred_website_source`, `wikidata`, `wikidata_qid`, `wikidata_class`, and
`area_km2`. The comparison schema retains Wikidata for overlap analysis and
`tags` retains every original tag. Existing v1.2 shards are projected in
bounded batches, atomically promoted, and reuploaded through their changed
content hash without PBF reads or website fetches.

## Boundedness and transactions

- Extraction keeps at most the configured row batch in each Python sink.
- Extraction also keeps at most the configured in-flight area payloads (32 by
  default, capped at 256). A bounded thread pool performs only pure geometry
  and record construction; FIFO draining preserves the sequential output
  contract and no live libosmium object or SQLite connection crosses a worker
  boundary. `area_workers` defaults to four and is capped at 16.
- Candidate and area-seen reconciliation lives in a per-PBF SQLite scratch
  file. It batches mutations behind a bounded commit interval and flushes on
  close; it is deleted after successful extraction and is not a resume
  checkpoint. Reads share the writer connection and see uncommitted rows, so
  reconciliation semantics are unchanged.
- Enrichment performs one bounded, parameterized SQLite lookup for each batch's
  unique normalized URLs (chunked below SQLite's variable limit), avoiding
  repeated reads for duplicate rows. It overlaps network retrieval for at most
  eight distinct misses per batch by default (`fetch_workers`, capped at 32).
  Trafilatura/lxml text extraction, the SQLite text cache, and row application
  stay on the caller thread because the native parser must not run concurrently
  on macOS. Trafilatura's configuration setup is reused per caller thread
  while the URL is refreshed for each document. Results remain deterministic in
  URL/row order without changing extracted text.
- Geographic aggregation reads only `lat` and `lon` columns in bounded batches,
  converts their Arrow buffers without Python list materialization, and keeps an
  explicit null mask so invalid-input errors remain fail-closed.
- Text-cache mutations commit in bounded batches and flush at every completed
  enrichment batch. Atomic, source-hash-bound Parquet parts retain completed
  prefixes across interruption; parts are assembled and removed only after the
  final shard promotion succeeds.
- Resume status checks scan only the two text-status columns with Arrow kernels,
  avoiding per-polygon Python row dictionaries while preserving the terminal
  `success`/`absent` contract.
- DuckDB has an explicit memory limit, one deterministic worker, and a
  run-owned spill directory.
- Source fingerprints are compared before and after reading.
- Per-source three-shard promotion and whole-analysis promotion restore the
  previous bundle on failure.
- The analysis stage writes into an invocation-owned, uniquely-named
  staging directory under `<run_dir>/staging/` and removes only that
  directory on success, ordinary exceptions, or `BaseException`. A
  leftover staging tree cannot block a later retry because each call
  uses a fresh directory name; unrelated diagnostic subdirectories of
  `<run_dir>/staging/` are never inspected or deleted. Hard process
  termination (e.g. `SIGKILL`) is outside the scope of these
  guarantees.
- Cleanup targets known temporary files only; non-empty diagnostic directories
  are retained.

## Resumable upload checkpoint (`uploaded_polygons.json`)

Operational resume state for partial Hugging Face uploads lives in
`manifests/uploaded_polygons.json`. The shape is exposed as the
`CheckpointV2` `TypedDict` (with `schema_version: Literal["v2"]`) and
parsed/validated by small helpers in `publishing/incremental.py`
(`_parse_checkpoint`, `_validate_sources_v2`, `_validate_legacy_sources`,
`_validate_global_bundle`, `_validate_hex_sha256`):

- `schema_version` is `Literal["v2"]` when the key is **present**. A
  missing key is the legacy case and migrates; a present-but-`null`
  value is rejected. Unknown schema versions fail closed with
  `ValueError("invalid uploaded polygon checkpoint: <reason>")`.
- Malformed JSON, non-UTF-8 bytes, malformed JSON root, unsupported
  schema version, unknown `global_bundle` field, malformed
  `map_contract_version` (string, bool, non-integer), non-hex
  `<name>.osm.pbf` source key, missing/invalid `polygon_sha256`,
  unknown per-source field, or malformed remote hash all raise the
  documented `ValueError`.
- `global_bundle` is a `TypedDict` (`_GlobalBundleStateV2`,
  `total=False`) with the four known keys
  `readme_sha256` / `dataset_yaml_sha256` / `map_sha256`
  (each a lowercase 64-character hex string) and
  `map_contract_version` (a non-bool integer). Empty or partial bundles
  are valid; any other field is rejected.
- `sources` maps `<name>.osm.pbf` filenames to
  `{"polygon_sha256": <64-char lowercase hex>}` records via the shared
  `_validate_hex_sha256` helper. Keys must end with `.osm.pbf`;
  per-source entries must contain **only** `polygon_sha256`.
- Legacy checkpoints (pre-`schema_version` flat dicts) are migrated
  silently only when every legacy entry is well-formed; any malformed
  entry raises `ValueError`.

The checkpoint file is operational state: it is excluded from the
completion receipt and from the publish plan, and remote SHA-256 hashes
are authoritative during apply-mode reconciliation.

`reconcile_upload_checkpoint` validates every remote SHA-256 and remote
filename **before** rewriting the checkpoint; a malformed remote hash
raises `ValueError("invalid uploaded polygon checkpoint: <reason>")`
and the existing `uploaded_polygons.json` is left byte-identical.

`load_upload_checkpoint` returns a fresh per-call dict and is safe to
mutate in place; shared mutable defaults are deliberately avoided to
keep resume state hermetic per run.

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
