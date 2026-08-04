# Pipeline

Implements bounded data-processing stages.

- Modules: `extraction`, `record_builders`, `enrich`, `public_schema_migration`, `analyze`,
  `partition_aggregate`.
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
lookups and SQLite writes stay on the caller thread, while distinct cache
misses use a bounded pool of eight I/O workers by default for network retrieval
and text extraction. `fetch_workers` can be configured per invocation up to a
safe cap of 32. Results are recorded and applied in deterministic URL and
input-row order; duplicate normalized URLs are fetched once per batch. Cache
commits are batched and flushed at each completed batch. Completed batches are
written as source-bound atomic checkpoint parts, so an interruption preserves
the shard prefix and retries only the unresolved suffix on resume.

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

It is computed once per builder invocation and never includes URL
classification or hostname extraction -- those are public-row-only and
remain in `extraction._public_record`.

`derive_wikidata` is a separate small helper that returns the normalized
`wikidata` value and its presence flag. It is used only by
`_comparison_record` and `_rejection_record`; the public shard's v1.3
schema omits Wikidata, so `_public_record` does not call it.
