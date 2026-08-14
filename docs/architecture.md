# Architecture

The repository contains code and tests only. Production PBFs are immutable
inputs under an explicitly supplied source root; generated run artifacts live
under a separate writable output root. `application` composes the phases, and
the lower-level packages keep their responsibilities independent.

## Package boundaries

| Package | Responsibility |
| --- | --- |
| `contracts` | Versioned Arrow schemas and text-status contracts. |
| `domain` | Deterministic OSM classification and geometry rules. |
| `storage` | Bounded, transactional local persistence. |
| `web` | Safe HTTP retrieval, Trafilatura extraction, and URL caching. |
| `runtime` | Configuration, paths, source safety, and run lifecycle. |
| `pipeline` | Extraction, enrichment, and analysis stages. |
| `reporting` | Artifact-derived cards, verification, and finalization. |
| `publishing` | Hugging Face credentials, checkpoints, and upload adapters. |
| `application` | Workflow composition, resume planning, and the Typer CLI. |

The architecture tests enforce the allowed dependency direction and reject
cycles. `application` is the composition layer; lower-level packages do not
import it.

## End-to-end flow

`run-all` discovers the complete PBF inventory and runs these phases in order:

1. **Inventory.** `init` records each filename, byte size, and nanosecond mtime
   in `expected_sources.json`. The output root must not be inside the source
   root.
2. **Extraction.** `extract` opens one inventoried PBF, assembles closed ways
   and supported polygon or boundary relations with libosmium, and writes one
   public, comparison, and rejection Parquet shard. Geometry and row
   construction use bounded workers; libosmium, SQLite, and Parquet remain
   callback-thread-owned. PBFs are processed sequentially.
3. **Enrichment.** `run-all` safely fetches both website-tag values, extracts
   full main text with Trafilatura, and migrates each public shard to schema
   v1.3. A run-owned SQLite cache reuses successes and retries unresolved
   statuses. Completed batches are source-bound checkpoint parts, so an
   interruption resumes at the first unfinished batch.
4. **Analysis.** `analyze-results` uses DuckDB with explicit memory and
   run-owned spill space to write the analysis Parquets. Each invocation has a
   private staging directory and promotes the complete bundle atomically.
5. **Card and map.** `build-card` recomputes the card from Parquet artifacts.
   The H3 resolution-3 density map counts each text-bearing public polygon
   centroid once and uses the bundled Natural Earth backdrop without network
   access.
6. **Verification and finalization.** `verify-results` checks source
   inventory, schemas, row invariants, independent counts and hashes, analysis
   files, card content, and map. `finalize-run` writes a SHA-256 completion
   receipt listing every publishable relative path, size, and hash.
7. **Publication.** `publish` is a read-only plan by default. With `--apply`,
   it re-verifies a complete run and uploads only receipt-bound artifacts to
   the configured Hugging Face dataset. `run-all --apply` may additionally
   upload each changed shard and refreshed card/map as progress; its final
   complete upload is still receipt-bound.

The phase commands are useful for inspection and recovery, but enrichment is
intentionally owned by `run-all`: it coordinates cache state, retry ordering,
durable parts, and per-source upload acknowledgements.

## Resume model

Repeating `run-all` with the same roots and run ID is safe. The source
fingerprints must still match; changed, duplicate, malformed, or missing
inventory entries fail closed. Existing complete extraction bundles are reused,
legacy v1.2 shards are migrated without reopening PBFs, terminal text statuses
(`success` and `absent`) are skipped, and retryable statuses are attempted
again. `Ctrl-C` leaves the run in a resumable state.

The local `manifests/uploaded_polygons.json` file records acknowledged remote
polygon hashes during apply mode. It is operational state, excluded from the
completion receipt, and reconciled against remote hashes before the next apply
upload. A receipt binds the final card, map, analysis, manifests, and Parquet
artifacts only after verification; staging, spill files, and checkpoint parts
are not publishable.

## Run layout

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
    uploaded_polygons.json       # operational; not receipt-bound
    completion_receipt.json      # written by finalize-run
  assets/geographic_polygon_density.png
  README.md
  dataset.yaml
```

Every source PBF receives one public shard, including a schema-valid empty
shard. The public Hugging Face split is `polygons/*.parquet`; comparison,
rejection, analysis, card, map, and receipt files are supporting artifacts. A
direct `publish --apply` upload is receipt-bound. `run-all --apply` can expose
incremental shard/card/map progress before completion, while analysis files
remain local until the final receipt-bound upload.

## Public data contract

A public row is an assembled closed way or supported polygon relation with a
non-empty `website` or `contact:website` tag. Wikidata is optional and remains
comparison-only. The versioned v1.3 schema stores full Trafilatura text and
exact Unicode `\w+` word counts independently for both website fields. It
removes the redundant `preferred_website`, `preferred_website_source`,
`wikidata`, `wikidata_qid`, `wikidata_class`, and `area_km2` columns; comparison
observations retain Wikidata, and `tags` retains every original OSM tag.

Card tables, combined word totals, hostname tables, and the map are derived
from the Parquets on each incremental update. The card intentionally omits
Hugging Face task metadata because this geographic source dataset does not map
to an official machine-learning task. Current public totals belong to the
generated card, not to this architecture overview.

## Boundedness and transactions

- Extraction keeps bounded Arrow row batches and at most the configured
  in-flight area payloads (32 by default, capped at 256). A four-worker geometry
  pool (capped at 16) preserves source order.
- Enrichment fetches at most eight distinct URL misses per batch by default
  (capped at 32). Cache commits and completed Parquet parts flush at each batch;
  row application remains ordered and single-threaded.
- Geographic aggregation reads only the needed columns in bounded batches.
  DuckDB uses one deterministic worker, an explicit memory limit, and a
  run-owned spill directory.
- Source shards, the analysis bundle, card/map assets, and receipts use atomic
  promotion. Known temporary files are cleaned without deleting unrelated
  diagnostic directories.
- A hard process kill such as `SIGKILL` is outside the interruption guarantees;
  ordinary exceptions and `KeyboardInterrupt` preserve resumable state.

## Provenance and licensing

The source code is Apache-2.0. Derived OpenStreetMap data is published under
ODbL 1.0 with OpenStreetMap contributor and Geofabrik attribution. The public
card and `dataset.yaml` carry those notices; source PBFs remain external,
read-only inputs and are never copied into the Git repository.
