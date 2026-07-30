# Interleaved Per-PBF Upload Design

## Goal

Reduce the time between durable Hugging Face checkpoints by completing each
source PBF through extraction, text enrichment, card recomputation, upload, and
checkpoint acknowledgement before starting the next source.

## Compatibility and Migration

The command, run ID, source inventory, output layout, Parquet schemas, URL
cache, source manifests, upload checkpoint, final analysis, completion receipt,
and Hugging Face repository paths remain unchanged.

An old-style run in `extracting` state is a supported migration input. For each
source, the workflow will first verify the existing three-shard bundle against
its source fingerprint, schemas, row counts, and hashes:

- a complete bundle is reused without opening its PBF;
- a missing, partial, or invalid bundle is extracted again for that source
  only;
- an already enriched current-schema polygon shard is not enriched again;
- a shard whose hash is already acknowledged in
  `uploaded_polygons.json` is not uploaded again;
- successful URL text already held in the SQLite cache remains reusable.

Therefore an existing partial run reuses every completed source. If execution
was interrupted during an atomic extraction, only that active source may need
to be extracted again.

## Per-Source Transaction

For each source in deterministic inventory order:

1. verify or extract its three local shards;
2. verify the bundle again before enrichment;
3. enrich the public polygon shard only when its schema or text statuses
   require enrichment;
4. atomically update public-shard row-count and hash metadata;
5. rebuild `README.md` and `dataset.yaml` from all currently finalized local
   Parquet shards;
6. compare the public shard hash with the upload acknowledgement;
7. when `--apply` is active and the hash is new, upload that shard and the
   recomputed card;
8. only after the remote upload succeeds, atomically persist its checkpoint.

Failure or `KeyboardInterrupt` propagates immediately. No later source starts,
no failed upload is acknowledged, and rerunning the identical command resumes
at the same source transaction.

## State Machine

The persisted run-level states remain unchanged for compatibility.

During `initialized` or `extracting`, per-source enrichment and upload may now
occur while the run-level state remains `extracting`. This state still means
the complete expected inventory has not finished its per-source transactions.

After every source transaction succeeds:

- transition `extracting -> extracted`;
- transition `extracted -> enriching`;
- transition `enriching -> enriched`.

The latter two transitions are immediate because enrichment has already been
completed per source. Existing runs encountered in `extracted` or `enriching`
state use the same per-source enrichment/upload routine without rereading PBFs.
Completed older runs requiring schema/text migration retain their existing
`complete -> enriching` migration path.

Analysis, final card generation, verification, finalization, and receipt-bound
full publication then proceed exactly as before.

## Counts and Progress

`WorkflowResult.extracted_count` continues to count only PBFs opened and
extracted during the current invocation. `skipped_count` continues to count
verified source bundles reused during the extraction phase.
`uploaded_count` continues to count only incremental uploads acknowledged
during the current invocation.

Existing progress message text remains stable. The order changes naturally to
`Extracting`, `Enriching`, and `Uploading` for one source before the next
source begins.

## Testing

Characterization and regression tests will cover:

- a fresh two-source run uploads source A before extracting source B;
- an old-style partially extracted run never rereads completed PBFs;
- an already enriched shard is reused;
- an acknowledged upload is not repeated;
- interruption after extraction resumes at enrichment without re-extraction;
- interruption during upload leaves the source unacknowledged and retries only
  that upload;
- dry-run performs no remote calls;
- existing complete-run migration still avoids PBF reads;
- all result counters and final state remain compatible.

At least one deliberate temporary mutation will prove the ordering test catches
the previous extract-all behavior.

## Operational Boundary

Implementation and tests do not read, stop, signal, or restart the active
production process and do not touch production PBFs or generated run data.
Because a running Python process has already imported the old workflow, it will
not adopt this change automatically. After the verified update is pushed, the
user can stop it gracefully and rerun the identical command to activate the
interleaved workflow while reusing completed artifacts.

## Acceptance Criteria

- One successful remote checkpoint is possible after each completed PBF.
- Previously extracted valid PBF bundles are reused.
- No public schema, manifest, path, CLI, or final publication contract changes.
- Failure and Ctrl-C remain safely resumable.
- Full Just, pre-commit, pre-push, pytest, Ruff, ty, build, installed-wheel, and
  GitHub Actions checks pass.
- The work is committed and pushed to the sole `main` branch.
