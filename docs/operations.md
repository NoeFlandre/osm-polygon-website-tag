# Operations and resume

This page covers the questions that matter when a run is large, interrupted,
or ready to publish. Keep code in the repository and point the CLI at a
separate writable output root.

## Choose safe roots

`--source-root` is the directory that contains the production `.osm.pbf`
files. It is an input only: the pipeline reads filenames, sizes, and mtimes,
and refuses an output root that is equal to or inside it. It does not copy,
rename, hash, or modify those PBFs.

`--output-root` contains one run directory per `--run-id`. The default generated
data root is `/Volumes/Seagate M3/projects/osm-polygon-website-tag`; set
`OSM_POLY_DATA_DIR` when a different local or mounted volume is appropriate.
Keep this root writable and outside the source tree. The production GlotLID
cache is `/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid/`;
language commands reject a run or model cache outside an approved Seagate root.
The old `…-data` root remains accepted only to resume or inspect existing runs;
new artifacts are written to the canonical project root.

## Resume `run-all`

Use the same source root, output root, run ID, and repository ID when resuming:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root '/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw' \
  --output-root '/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs' \
  --run-id 'geofabrik-website-v1'
```

The first invocation records the recursively discovered inventory. Each later
invocation compares every filename, byte size, and nanosecond mtime with that
inventory; drift fails closed instead of silently mixing inputs. Pressing
`Ctrl-C` leaves the run resumable. Repeating the command reuses verified
extraction bundles, skips terminal text results, and retries unresolved or
failed URL results.

After `finalize-snapshot` writes the completion receipt, the run is frozen:
`status=complete` plus `snapshot_status=done` makes a later `run-all` invocation
return immediately. It does not rediscover PBFs, retry failed URLs, rebuild the
card, or upload anything. Start a separate reviewed run if you ever decide to
attempt additional enrichment.

Enrichment writes completed Parquet batches and URL-cache changes durably. An
interruption therefore resumes at the first unfinished batch rather than
starting the shard over. Existing schema-v1.2 shards are projected to v1.3
locally without reopening the PBF or refetching text. The Parquet text-status
columns are authoritative; a small status summary only helps choose resume
order.

## Optional GlotLID language detection

The default `run-all` command remains schema v1.3 and does not load a model.
Enable the stage explicitly:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root '/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw' \
  --output-root '/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs' \
  --run-id 'geofabrik-website-v1' \
  --detect-languages
```

Or run language detection after an existing run has reached `enriched`:

```bash
uv run --locked osm-polygon-website-tag detect-languages \
  --run-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/geofabrik-website-v1'
```

The stage uses the pinned [GlotLID model](https://huggingface.co/cis-lmu/glotlid)
(`model_v3.bin` at a fixed Hub revision). It loads one model into one process,
predicts bounded batches, and writes four nullable v1.4 fields:
`website_language`, `website_language_probability`,
`contact_website_language`, and `contact_website_language_probability`.
Successful text receives the exact script-aware top-1 label (for example
`eng_Latn`) and probability; absent or unsuccessful text remains null.

Each public shard has source- and model-bound `.language.parts` checkpoints.
After every completed batch is atomically written, the next batch can be
processed. `Ctrl-C` is safe: the original shard stays valid, the completed
prefix remains on Seagate, and repeating the command verifies identity and
continues from the first unfinished batch. A changed shard or model fails
closed. Once every shard is promoted, the source manifest hashes are updated.

If the standalone stage is run on an analyzed, card-built, or non-frozen
complete run, it reopens the stage as `enriching` and finishes at `enriched`.
Rebuild analysis, card, verification, and finalization afterward. A snapshot
with `status=complete` and `snapshot_status=done` is immutable and is rejected
before model loading. Tests use injected fakes and `tmp_path`; they never
download GlotLID or write production data.

## Run language detection on Grid'5000

The repository includes wrappers in `scripts/grid5000/` for short, resumable
jobs. They follow the Grid'5000 usage policy: the frontend is used only for
checkout, transfer, submission, and monitoring; detection runs on one
reserved GPU node. Each job requests `host=1/gpu=1,walltime=0:30` and
gives the detector a 1,500-second budget. The remaining five minutes are
reserved for job cleanup and transfer. GlotLID's pinned FastText model is
CPU-bound; the GPU reservation provides the requested isolated workers and
parallel-job capacity, but does not claim GPU acceleration for inference.

First download the pinned public model into the Seagate cache and prepare one
new bundle. Preparation records the repository commit, source row count and
hash, model revision and hash, and batch/budget settings. It copies exactly
one unfinished public shard and any validated language checkpoint prefix:

```bash
hf download cis-lmu/glotlid model_v3.bin \
  --revision 85cd671 \
  --cache-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid'

export OSM_POLY_RUN_DIR='/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>'
export OSM_POLY_BUNDLE_DIR='/Volumes/Seagate M3/projects/osm-polygon-website-tag/grid5000/<bundle-id>'
export OSM_POLY_MODEL_PATH='/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid/<snapshot>/model_v3.bin'
export OSM_POLY_COMMIT="$(git rev-parse HEAD)"
scripts/grid5000/prepare_language_detection.sh
```

Copy the checkout and bundle to the selected site's frontend with `rsync`.
Before the first detection job, bootstrap the locked Linux runtime once by
pointing the same policy-aware submit wrapper at
`scripts/grid5000/bootstrap_language_runtime.sh`. Wait for that job to finish
and clear its active marker only after confirming its terminal state. The
bootstrap installs runtime dependencies without the development group; all
subsequent detection jobs use that environment offline. Then submit one job
with `scripts/grid5000/submit_language_detection.sh`; it runs
`usagepolicycheck -t` before and after `oarsub`, records the OAR job ID, and
refuses to submit while its active marker exists. Set `GRID5000_GPUS=1` per
job; submit distinct bundles one at a time or in a small staged wave, never
duplicate or speculative jobs. Monitor with `oarstat -u`.
Do not run Python, model inference, compilation, or bulk processing on the
frontend. The node runner sets `HF_HUB_OFFLINE=1`, uses only the staged model
and shard, and never fetches website URLs or Hub weights.

After completion, copy the bundle back to Seagate and run
`scripts/grid5000/sync_language_detection.sh`. Checkpoint-only results leave
the canonical shard byte-identical and can be resumed by preparing a fresh
bundle. Completed results are checksum- and schema-validated before atomic
promotion and manifest update. Retain the Seagate bundle and receipt as
provenance; clean temporary Grid'5000 copies only after the receipt and
checksums have been verified. Cancel an unneeded job with `oardel <job-id>` and
clear its marker only after confirming that it is no longer running.

## Dry run versus apply

Without `--apply`, `run-all` computes and verifies local artifacts but does not
upload to Hugging Face. `publish` and `publish-plan` are also read-only by
default. This is the review path: inspect the run, card, map, and plan before
granting publication approval.

With `--apply`, credentials are resolved from `HF_TOKEN`,
`HUGGING_FACE_HUB_TOKEN`, or the local Hugging Face credential store. The
workflow reconciles the local upload checkpoint with remote polygon hashes,
uploads an enriched shard and refreshed card together, and records the
acknowledgement before continuing. At the end it uploads only the files bound
by the completion receipt. A missing credential, changed source inventory, or
failed verification stops the run before the corresponding upload.

## What is local and what is public

The run directory is local until apply-mode publication succeeds. It contains
public polygon shards plus comparison, rejection, analysis, card, map, and
manifest artifacts. `manifests/uploaded_polygons.json` is operational resume
state and is excluded from the completion receipt. Staging and spill files are
also excluded.

The public Hugging Face dataset is the subset selected by the verified receipt:
`polygons/*.parquet`, receipt-bound analysis and supporting artifacts, the
generated card, and the map when present. A local run can contain newer or
incomplete work than the public dataset; do not describe local-only files as
published until the apply-mode upload has completed and been checked.

## Safe publication checklist

1. Confirm the source root is unchanged and the run is complete.
2. Run `verify-results --run-dir '<run-dir>'` and review the generated card.
3. Run `publish-plan --run-dir '<run-dir>'` and inspect its artifact list.
4. Obtain separate approval, authenticate with `hf auth login`, and add
   `--apply` to `publish` (or to the reviewed `run-all` command).

The publisher re-runs verification immediately before any upload and refuses
partial or tampered runs. Keep the source mount read-only in Docker and pass a
token through the environment only; never place credentials in a file copied
into an image or in a command-line argument.

## Freeze the current data without more retries

If you want the current extracted/text results to be the final snapshot, do
not resume `run-all`: it intentionally retries unresolved URL outcomes. With a
reviewed `snapshot_status: done` marker in `manifests/run.json`, use:

```bash
uv run --locked osm-polygon-website-tag finalize-snapshot \
  --run-dir '<run-dir>'
```

This command reads the existing shards only. It refuses unfinished `pending`
text rows, preserves recorded fetch failures and deterministic URL rejections,
then builds analysis tables, the card and map, verifies every artifact, and
writes the completion receipt. It performs no PBF reads, URL requests, or
enrichment retries.

## Publish the metrics dashboard

The dataset card links to the public
[Trackio Space](https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics).
Its metrics come from the same `CardStats` projection as the card and are
published only from a run with `status=complete`, a valid completion receipt,
and a fresh successful verification.
Preview them without a network call:

```bash
uv run --locked osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>'
```

After reviewing the output, the explicit remote action is:

```bash
uv run --with trackio osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>' \
  --space-id 'NoeFlandre/osm-polygon-website-tag-metrics' \
  --apply
```

The optional Trackio client creates or refreshes a public, read-only static
Space when needed. It logs one receipt-named run and sends only aggregate
numeric dataset metrics plus a SHA-256 snapshot identifier; no website text or
credential is sent to the dashboard. This command is separate from `run-all`,
so the normal PBF pipeline remains unaffected if the dashboard is unavailable.
