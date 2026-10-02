# Architecture

The repository contains only code and tests. The production PBFs are immutable inputs under an explicitly supplied source root. The generated run artifacts are under a separate writable output root. `application` composes the phases. The lower-level packages keep their responsibilities independent.

## Package boundaries

| Package | Responsibility |
| --- | --- |
| `contracts` | Versioned Arrow schemas, language fields, and text-status contracts. |
| `domain` | Deterministic OSM classification and geometry rules. |
| `storage` | Bounded, transactional local persistence. |
| `web` | Safe HTTP retrieval, Trafilatura extraction, and URL caching. |
| `runtime` | Configuration, paths, source safety, and run lifecycle. |
| `pipeline` | Extraction, enrichment, language detection, and analysis stages. |
| `reporting` | Cards from artifacts, verification, and finalization. |
| `publishing` | Hugging Face credentials, checkpoints, and upload adapters. |
| `application` | Workflow composition, resume planning, and the Typer CLI. |

The architecture tests enforce the allowed direction of dependencies. They reject cycles. `application` is the composition layer. The lower-level packages do not import it.

## End-to-end flow

`run-all` discovers the complete PBF inventory. It runs these phases in this order:

1. **Inventory.** `init` records the filename, the byte size, and the nanosecond mtime of each source in `expected_sources.json`. The output root must not be inside the source root.
2. **Extraction.** `extract` opens one inventoried PBF. It assembles closed ways and supported polygon or boundary relations with libosmium. It writes one public Parquet shard, one comparison Parquet shard, and one rejection Parquet shard. Bounded workers do the geometry and the row construction. The callback thread owns libosmium, SQLite, and Parquet. The pipeline processes the PBFs one after the other.
3. **Enrichment.** `run-all` safely fetches both website-tag values. It extracts the full main text with Trafilatura. It migrates each public shard to schema v1.3. A SQLite cache that the run owns reuses the successes and retries the unresolved statuses. The completed batches are checkpoint parts that are bound to the source. So an interruption resumes at the first unfinished batch.
4. **Optional language detection.** When you enable `--detect-languages`, `run-all` or `detect-languages` loads one pinned GlotLID model from the model cache in the data root. It predicts the successful website texts in bounded batches. It upgrades each public shard to schema v1.4 in one atomic step. The default workflow does not load the model.
5. **Analysis.** `analyze-results` uses DuckDB with an explicit memory limit and a spill space that the run owns. It writes the analysis Parquets. Each invocation has a private staging directory. It promotes the complete bundle in one atomic step.
6. **Card and map.** `build-card` computes the card again from the Parquet artifacts. The H3 resolution-3 density map counts the centroid of each text-bearing public polygon one time. It uses the bundled Natural Earth backdrop and needs no network access.
7. **Verification and finalization.** `verify-results` checks the source inventory, the schemas, the row invariants, the independent counts and hashes, the analysis files, the card content, and the map. `finalize-run` writes a SHA-256 completion receipt. The receipt lists the relative path, the size, and the hash of each publishable file.
8. **Publication.** `publish` is a read-only plan by default. With `--apply`, it verifies a complete run again. It uploads only receipt-bound artifacts to the configured Hugging Face dataset. `run-all --apply` can also upload each changed shard and the refreshed card and map as progress. Its final complete upload is still receipt-bound.

The phase commands help with inspection and recovery. `run-all` owns the enrichment on purpose. It coordinates the cache state, the retry order, the durable parts, and the upload acknowledgements for each source.

## Resume model

It is safe to repeat `run-all` with the same roots and the same run ID. The source fingerprints must still match. The command fails closed on an inventory entry that is changed, duplicate, malformed, or missing. The command reuses the existing complete extraction bundles. It migrates legacy v1.2 shards without a reopen of the PBFs. It skips the terminal text statuses (`success` and `absent`). It tries the retryable statuses again. `Ctrl-C` leaves an active run in a resumable state. After you explicitly freeze a completed snapshot (`snapshot_status=done` and a completion receipt), `run-all` treats it as immutable. It does not retry or upload.

When you enable language detection, each shard also has language checkpoint parts under `.language.parts`. The parts are bound to the source and to the model. The command keeps the original shard until it assembles all language rows and validates them as v1.4. So an interruption resumes only the unfinished prefix. The standalone language command rejects frozen snapshots. It skips the model loading when all language pairs are complete.

In apply mode, the local file `manifests/uploaded_polygons.json` records the acknowledged remote polygon hashes. It is operational state. The completion receipt excludes it. Before the next apply upload, the command reconciles it with the remote hashes. After verification, a receipt binds the final card, the map, the analysis, the manifests, and the Parquet artifacts. The staging files, the spill files, and the checkpoint parts are not publishable.

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
  stats.json                     # geometry statistics; written by build-card
```

The shared production data root also contains `models/glotlid/model_v3.bin` and its Hugging Face cache metadata. The language checkpoints are local operational state. They are not receipt-bound and not public.

Each source PBF gets one public shard. This includes a schema-valid empty shard. The public Hugging Face split is `polygons/*.parquet`. The comparison, rejection, analysis, card, map, and receipt files are supporting artifacts. A direct `publish --apply` upload is receipt-bound. `run-all --apply` can show incremental shard, card, and map progress before completion. The analysis files stay local until the final receipt-bound upload.

## Public data contract

A public row is an assembled closed way or a supported polygon relation. It has a non-empty `website` or `contact:website` tag. Wikidata is optional and stays comparison-only. The versioned v1.3 schema stores the full Trafilatura text and the exact Unicode `\w+` word counts. It stores them separately for both website fields. It removes the redundant columns `preferred_website`, `preferred_website_source`, `wikidata`, `wikidata_qid`, `wikidata_class`, and `area_km2`. The comparison observations keep Wikidata. `tags` keeps each original OSM tag. The opt-in v1.4 schema appends the nullable fields `website_language` and `website_language_probability`. It also appends the two corresponding `contact_website_*` fields. They contain the exact GlotLID script-aware label and the top-1 probability for successful text. They are null when no successful text is available.

The pipeline derives these items from the Parquets at each incremental update: the card tables, the combined word totals, the hostname tables, the map, and the `stats.json` geometry and text report. The geometry block of the card and `stats.json` use the same geometry population. Their text and map fields use the same global reducer. So these artifacts cannot disagree silently. Read [Polygon geometry statistics](data-and-remotes.md#polygon-geometry-statistics). The card omits the Hugging Face task metadata on purpose. This geographic source dataset does not map to an official machine-learning task. The generated card holds the current public totals. This architecture overview does not hold them.

## Boundedness and transactions

- Extraction keeps bounded Arrow row batches. It keeps at most the configured number of in-flight area payloads (32 by default, maximum 256). A geometry pool of four workers (maximum 16) keeps the source order.
- By default, enrichment fetches at most eight distinct URL misses for each batch (maximum 32). The cache commits and the completed Parquet parts flush at each batch. The row application stays ordered and single-threaded.
- Language detection loads one model for the whole process. It predicts at most the configured bounded row batch at one time. The checkpoint parts and the final assembly stream through Parquet. They do not load a complete shard into memory.
- Geographic aggregation reads only the necessary columns in bounded batches. DuckDB uses one deterministic worker, an explicit memory limit, and a spill directory that the run owns.
- The source shards, the analysis bundle, the card and map assets, and the receipts use atomic promotion. The command cleans known temporary files. It does not delete unrelated diagnostic directories.
- A hard process kill, such as `SIGKILL`, is outside the interruption guarantees. Ordinary exceptions and `KeyboardInterrupt` keep a resumable state.

## Provenance and licensing

The source code is Apache-2.0. The derived OpenStreetMap data is published under ODbL 1.0, with OpenStreetMap contributor attribution and Geofabrik attribution. The public card and `dataset.yaml` carry these notices. The source PBFs stay external, read-only inputs. Nobody copies them into the Git repository.
