# Language Readiness Fast Path Design

## Goal

Reduce the I/O and Python allocation overhead of the opt-in GlotLID stage
without changing any public shard bytes, language predictions, checkpoints,
errors, or resume behavior.

## Evidence

The current detection path can inspect a large v1.4 shard multiple times before
processing it:

1. the application checks `shard_needs_language_detection`;
2. `detect_language_shard` validates text statuses;
3. its context preparation calls `shard_needs_language_detection` again; and
4. detection reads the source rows to build checkpoint parts.

The readiness check also converts every projected row into a Python dictionary
with `RecordBatch.to_pylist()`, although it only needs six scalar columns. A
synthetic 100,000-row completed shard took 0.308 seconds for the current
readiness check on the development machine.

## Design

Add one internal bounded inspection function in
`pipeline/detect_languages.py`. It will validate the two text-status columns
and determine whether a shard needs language work in the same Arrow batch
iteration. Legacy v1.3 shards still return `True` after status validation;
current v1.4 shards inspect only the status and language columns. The existing
private status-validation helpers and public `shard_needs_language_detection`
function remain available and preserve their current contracts.

`detect_language_shard` will use the already-open `ParquetFile` and the single
inspection result when preparing its context. The source-processing caller will
invoke `detect_language_shard` directly when language detection is enabled;
the detector already has a no-op result for complete shards, so the caller no
longer performs a duplicate readiness scan. The CLI's explicit list-building
precheck remains unchanged because it must avoid loading the model when every
shard is complete.

Readiness predicates remain exactly equivalent: successful text needs a
non-empty language label and a probability in the closed unit interval;
non-successful text must have both language fields null. Detection remains
bounded by Arrow batches, model calls, checkpoint parts, and atomic promotion.

## Testing and verification

RED tests will prove the internal inspection requests one bounded column batch,
validates terminal statuses, preserves the v1.3/v1.4 readiness predicates, and
that source processing delegates complete-shard no-op handling to the detector.
GREEN will implement only the inspection/reuse path. Existing language,
checkpoint, workflow, and CLI tests must remain green. A synthetic benchmark
will compare the old and new inspection behavior without asserting a
machine-dependent wall-clock threshold; it will record scan count and elapsed
time. Full `just check`, `just pre-commit`, `just pre-push`, CRAP, and scoped
mutation verification are required before commit and push.

## Non-goals

- changing language labels, probabilities, model files, or inference batching;
- changing Parquet schemas, row order, checkpoint format, or error messages;
- changing default worker counts or adding concurrency;
- reading production PBFs, running production inference, or publishing data;
- modifying unrelated user-owned working-tree files.
