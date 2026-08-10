# Pipeline

Implements bounded data-processing stages.

- Modules: `extraction`, `record_builders`, `enrich`, `enrichment_checkpoint`,
  `public_schema_migration`, `analyze`, `partition_aggregate`.
- Dependencies: `contracts`, `domain`, `storage`, `web`, and `runtime`.
- Entry points: `extract_pbf`, `enrich_polygon_shard`,
  `migrate_public_shard`, `analyze_results`.
- Excludes: full-run orchestration, card rendering, and remote publication.

## Extraction

`extract_pbf` keeps libosmium callbacks, the candidate ledger, and Parquet
sinks on the caller thread. Each qualifying area is copied to a serialized
GeoJSON payload and processed by a bounded FIFO pool for geometry metrics and
row construction. The default is four area workers with at most 32 in-flight
payloads; safe caps are 16 workers and 256 in-flight payloads. Results are
emitted in callback order, so worker-count changes preserve row order, schemas,
rejections, and shard hashes. PBFs themselves remain sequential in
`application.workflow`.

## Enrichment

`enrich_polygon_shard` processes one bounded Arrow batch at a time. Cache
lookups, SQLite writes, and Trafilatura/lxml text extraction stay on the caller
thread. Each batch performs a parameterized, chunked lookup for its unique
normalized URLs, so repeated rows do not issue repeated SQLite reads. Distinct
cache misses use a bounded pool of eight I/O workers by default for network
retrieval only; keeping native HTML parsing serial avoids
platform-specific lxml allocator failures without changing extracted text.
`fetch_workers` can be configured per invocation up to a safe cap of 32.
Results are recorded and applied in deterministic URL and input-row order;
duplicate normalized URLs are fetched once per batch. Cache commits are
batched and flushed at each completed batch. Completed batches are written as
source-bound atomic checkpoint parts, so an interruption preserves the shard
prefix and retries only the unresolved suffix on resume.

`enrich` owns row orchestration, URL resolution, cache use, and final shard
promotion. `enrichment_checkpoint` owns the source-bound checkpoint metadata,
sequential part validation, atomic part writes, and bounded final assembly.
This boundary keeps durable resume-state rules independently reviewable while
leaving the `enrich_polygon_shard` entry point and on-disk contract unchanged.

## Record builders

`record_builders` is a pure `domain`-only helper that factors out the
tag-derived values reused by the extraction row builders.

`derive_tags` is the projection shared by all three row builders
(`_public_record`, `_comparison_record`, `_rejection_record`). It returns
exactly:

- normalized `website` and `contact:website`,
- the website presence flags (`has_website`, `has_contact_website`,
  `has_any_website`),
- the primary category.

Production extraction computes it once per area payload and passes the frozen
projection to all row builders. Direct builder calls may omit the optional
projection and retain the same derive-on-demand behavior. It never includes
URL classification or hostname extraction -- those are public-row-only and
remain in `extraction._public_record`.

`derive_wikidata` is a separate small helper that returns the normalized
`wikidata` value and its presence flag. It is used only by
`_comparison_record` and `_rejection_record`; the public shard's v1.3
schema omits Wikidata, so `_public_record` does not call it.

## Analysis

`analyze_results` computes every table in `<run_dir>/analysis/` from the
finished polygons, comparison observations, and rejections shards. The
analyzer uses DuckDB external memory, one deterministic worker, and a
run-owned spill directory under `<run_dir>/staging/duckdb/`. DuckDB writes
large results directly to Parquet so the bytes are deterministic across
runs.

### Crash-safe staging lifecycle

Every invocation writes into its own unique staging directory under
`<run_dir>/staging/`, created via `tempfile.mkdtemp(prefix="analysis-",
dir=staging_root)`. The complete `analysis/*.parquet` bundle is then
atomically promoted into `<run_dir>/analysis/` through
`atomic_promote_bundle`, preserving the existing all-old-or-all-new
contract: a failed analysis never partially replaces the previous bundle.

The per-invocation staging directory is removed on success, on ordinary
exceptions, and on `BaseException` (including `KeyboardInterrupt`). The
cleanup logic intentionally targets only the directory created by the
current invocation -- it never inspects, reuses, or recursively deletes
unrelated subdirectories of `<run_dir>/staging/`. A pre-existing
diagnostic or mis-named directory (for example the legacy fixed name
`analysis-build`) is left untouched, and a later retry is safe to call
without manual cleanup. Cleanup failures are suppressed so they never mask
the original analysis exception; a leftover per-invocation tree cannot
block the next call because every retry creates a freshly-named directory.

Hard process termination (such as `SIGKILL`) is outside the scope of these
guarantees; an in-flight DuckDB write may leave spill files under
`staging/duckdb/` because `duckdb_engine.cleanup_temp_dir` preserves
non-empty diagnostic directories on purpose.
