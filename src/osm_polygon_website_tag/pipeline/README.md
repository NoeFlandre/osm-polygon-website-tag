# Pipeline

Implements bounded data-processing stages.

- Modules: `extraction`, `extraction_handler`, `area_work`, `extraction_records`,
  `record_builders`, `enrich`, `checkpoint_storage`, `enrichment_checkpoint`, `glotlid`,
  `language_detection_checkpoint`, `detect_languages`, `model_identity`,
  `sat`, `sentence_languages`, `sentences`, `sentence_checkpoint`,
  `split_sentences`, `sentence_run`, `grid5000_bundle`, `grid5000`,
  `grid5000_sentences`,
  `public_schema_migration`, `analyze`, `partition_aggregate`.
- Dependencies: `contracts`, `domain`, `storage`, `web`, and `runtime`.
- Entry points: `extract_pbf`, `enrich_polygon_shard`,
  `migrate_public_shard`, `detect_language_shard`, `segment_sentence_shard`,
  `analyze_results`, `deduplicate_public_shards`.
- Excludes: full-run orchestration, card rendering, and remote publication.

`deduplicate_public_shards` is a read-only derivative stage. It reads finalized
public shards, selects one complete row per `(osm_type, osm_id)` using OSM
version, timestamp, and stable source-name tie-breakers, then writes the winner
back to the same source-named shard (including empty expected shards). The
winner's website, geometry, and extracted text are kept together; values from
discarded snapshots are never merged into it. The original source-level run is
not modified, and the output is promoted only after every shard satisfies the
public schema.

## Extraction

`extract_pbf` keeps libosmium callbacks, the candidate ledger, and Parquet
sinks on the caller thread. Each qualifying area is copied to a serialized
GeoJSON payload and processed by a bounded FIFO pool for geometry metrics and
row construction. The default is four area workers with at most 32 in-flight
payloads; safe caps are 16 workers and 256 in-flight payloads. Results are
emitted in callback order, so worker-count changes preserve row order, schemas,
rejections, and shard hashes. PBFs themselves remain sequential in
`application.workflow`.

`extraction_handler` owns libosmium callbacks, candidate reconciliation,
area-task orchestration, and bounded shard sinks. `extraction` owns source
validation, run-state updates, and atomic promotion, while re-exporting the
established worker constants, payload/result types, handler names, and private
row-builder aliases for compatibility. `area_work` owns only the copied
payload/result contracts, validated worker bounds, and bounded FIFO executor;
it has no libosmium, SQLite, or Parquet dependency. `extraction_records` owns
the deterministic, side-effect-free construction of public, comparison, and
rejection rows.

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

The workflow classifies retryable shards from the two status columns with
bounded Arrow batches and persists the small per-source counts in
`manifests/sources.json`. Resumes therefore prioritize unfinished rows and
transient fetch/extraction failures, then deterministic invalid/unsafe URL
failures; a shard with durable checkpoint parts is always handled before an
ordinary retry. Sources without an extraction bundle remain first. Missing
summaries from older runs are backfilled once at the resume boundary without
opening any PBF. A completed run explicitly frozen with `snapshot_status: done`
and a completion receipt is immutable; `run-all` returns without reopening
sources or retrying enrichment.

`enrich` owns row orchestration, URL resolution, cache use, and final shard
promotion. Durable resume state belongs to `checkpoint_storage`, whose
`CheckpointStore` owns every mechanic a resumable stage needs: part naming and
ordering, temporary-file cleanup, the stored identity contract, rejection of
unrecognized directory contents, atomic part promotion, and bounded final
assembly. A stage declares its contract once and then works in whole
checkpoints. `enrichment_checkpoint` declares only what is specific to
enrichment — its directory suffix, error identity, and the target schema
resolved per invocation — so the on-disk contract and the
`enrich_polygon_shard` entry point are unchanged.

## Language detection

`detect_language_shard` consumes a public v1.3 or v1.4 shard only after both
text status columns are terminal. It calls the injected `LanguageDetector` for
bounded, independent website and contact-text batches, preserves completed
language pairs, and atomically promotes a validated v1.4 shard. Absent or
unsuccessful text receives null language fields.

`validate_language_detection_options` shares the batch-size and time-budget
checks between shard detection and the CLI, before either opens run artifacts.

`glotlid` owns the pinned FastText/GlotLID adapter, model hash, and explicit
Seagate cache boundary. `language_detection_checkpoint` declares the language
stage's `CheckpointStore` and joins the pinned model identity to the source
hash in the stored contract, so labels are only reused while both are
unchanged; the checkpoint mechanics themselves live in `checkpoint_storage`
and are shared with enrichment. A `Ctrl-C` or ordinary
exception leaves the source shard untouched and preserves durable parts for
the next invocation. The model is loaded once by the application layer and is
never copied to URL or geometry workers.

`grid5000_bundle` holds what both staged stages share: the bundle and receipt
payload primitives, the pinned model identity, and the directory creation and
replacement helpers that keep a synchronization crash from destroying prior
work.

`grid5000` owns the portable bundle and result-receipt boundary. Preparation
copies one unfinished shard, its validated checkpoint prefix, and the pinned
model into a new Seagate bundle. The reserved-node runner reads only those
files and stays offline; synchronization validates the receipt and atomically
installs either the checkpoint prefix or the completed v1.4 shard back into
the canonical run.

`grid5000_sentences` is the same boundary for segmentation, with one
difference: a bundle carries several shards packed up to a row budget, and its
receipt records every shard the job touched, so synchronization installs the
completed v1.5 shards and at most one paused checkpoint. `sentence_run` owns
the bounded multi-shard loop those jobs share with the local command: one
monotonic budget across shards, stopping cleanly between or inside a shard.

## Record builders

`record_builders` is a pure `domain`-only helper that factors out the
tag-derived values reused by the three builders in `extraction_records`.

`derive_tags` is the projection shared by `build_public_record`,
`build_comparison_record`, and `build_rejection_record`. It returns
exactly:

- normalized `website` and `contact:website`,
- the website presence flags (`has_website`, `has_contact_website`,
  `has_any_website`),
- the primary category.

Production extraction computes it once per area payload and passes the frozen
projection to all row builders. Direct builder calls may omit the optional
projection and retain the same derive-on-demand behavior. It never includes
URL classification or hostname extraction -- those are public-row-only and
remain in `extraction_records.build_public_record`.

`derive_wikidata` is a separate small helper that returns the normalized
`wikidata` value and its presence flag. It is used only by
`build_comparison_record` and `build_rejection_record`; the public shard's
v1.3 schema omits Wikidata, so `build_public_record` does not call it.

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

## Sentence segmentation

`segment_sentence_shard` consumes a language-complete v1.4 shard (or an
already-segmented v1.5 one) and atomically promotes a validated v1.5 shard.
Segmentation is attempted only for text the fetch stage completed, that is
non-blank, and whose detected language the model covers; every other row
records why it was skipped -- `absent`, `empty_text`, or
`unsupported_language` -- rather than carrying an indistinguishable null.

`sat` owns the pinned SaT ("Segment any Text") adapter. The model is a
Hugging Face directory rather than one binary, so its identity is a digest
over every file, with relative paths folded in so a reshuffled repository
reads as a different model. Its revision is supplied by the caller rather
than hard-coded, because nothing in the tree may claim a commit that has not
actually been staged. `wtpsplit` is imported lazily inside the loader so that
importing the adapter never drags in a deep-learning backend.

`sentence_languages` bridges the two models' naming: GlotLID labels a
language as `<ISO 639-3>_<script>` and covers thousands, while the segmenter
is trained on 85 named by ISO 639-1. The segmenter is script-agnostic at
inference, so only the language subtag is resolved and the script half is
deliberately ignored. A guard test asserts the pinned table still matches the
language metadata `wtpsplit` actually ships.

`sentence_checkpoint` binds the durable prefix to the segmentation model as
well as the source hash, so sentences are reused only while both are
unchanged; the checkpoint mechanics themselves are `checkpoint_storage`'s,
shared with enrichment and language detection. A time budget pauses between
batches and leaves the source shard and its durable prefix intact, which is
what makes a walltime-bounded reservation resumable.
