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
data root is `/Volumes/Seagate M3/projects/osm-polygon-website-tag-data`; set
`OSM_POLY_DATA_DIR` when a different local or mounted volume is appropriate.
Keep this root writable and outside the source tree.

## Resume `run-all`

Use the same source root, output root, run ID, and repository ID when resuming:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root '/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw' \
  --output-root '/Volumes/Seagate M3/projects/osm-polygon-website-tag-data/runs' \
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
