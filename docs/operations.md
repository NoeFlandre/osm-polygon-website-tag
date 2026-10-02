# Operations and resume

This page answers the questions that matter when a run is large, is interrupted, or is ready to publish. Keep the code in the repository. Point the CLI at a separate writable output root.

## Choose safe roots

`--source-root` is the directory that contains the production `.osm.pbf` files. It is an input only. The pipeline reads the filenames, the sizes, and the mtimes. It refuses an output root that is equal to the source root, or inside it. It does not copy, rename, hash, or modify those PBFs.

`--output-root` contains one run directory for each `--run-id`. The generated-data root comes from `OSM_POLY_DATA_DIR` (environment or `.env`). The default is `./data`. Keep it writable and outside the source tree. The GlotLID cache is `$OSM_POLY_DATA_DIR/models/glotlid/`. The language commands reject a run outside the data root.

## Resume `run-all`

When you resume, use the same source root, output root, run ID, and repository ID:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root /path/to/pbf-root \
  --output-root "${OSM_POLY_DATA_DIR:-./data}/runs" \
  --run-id 'geofabrik-website-v1'
```

The first invocation records the inventory that it discovers recursively. Each later invocation compares each filename, byte size, and nanosecond mtime with that inventory. A drift fails closed. The command does not mix inputs silently. When you press `Ctrl-C`, the run stays resumable. When you repeat the command, it reuses the verified extraction bundles. It skips the terminal text results. It retries the unresolved or failed URL results.

After `finalize-snapshot` writes the completion receipt, the run is frozen. With `status=complete` and `snapshot_status=done`, a later `run-all` invocation returns immediately. It does not discover the PBFs again. It does not retry failed URLs. It does not rebuild the card. It does not upload anything. If you decide to try more enrichment, start a separate reviewed run.

Enrichment writes completed Parquet batches and URL-cache changes in a durable way. So an interruption resumes at the first unfinished batch. It does not start the shard again. The command projects existing schema-v1.2 shards to v1.3 locally. It does not reopen the PBF and does not fetch the text again. The text-status columns of the Parquet are authoritative. A small status summary only helps to choose the resume order.

## Optional GlotLID language detection

The default `run-all` command stays at schema v1.3. It does not load a model. Enable the stage explicitly:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root /path/to/pbf-root \
  --output-root "${OSM_POLY_DATA_DIR:-./data}/runs" \
  --run-id 'geofabrik-website-v1' \
  --detect-languages
```

Or run language detection after an existing run reaches `enriched`:

```bash
uv run --locked osm-polygon-website-tag detect-languages \
  --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/geofabrik-website-v1"
```

The stage uses the pinned [GlotLID model](https://huggingface.co/cis-lmu/glotlid) (`model_v3.bin` at a fixed Hub revision). It loads one model into one process. It predicts bounded batches. It writes four nullable v1.4 fields: `website_language`, `website_language_probability`, `contact_website_language`, and `contact_website_language_probability`. Successful text gets the exact script-aware top-1 label (for example `eng_Latn`) and the probability. Absent or unsuccessful text stays null.

Each public shard has `.language.parts` checkpoints. They are bound to the source and to the model. After the command writes each completed batch in an atomic step, it can process the next batch. `Ctrl-C` is safe. The original shard stays valid. The completed prefix stays in the local run. When you repeat the command, it verifies the identity and continues from the first unfinished batch. A changed shard or a changed model fails closed. After the command promotes every shard, it updates the source manifest hashes.

A later URL retry can change a failed value into `success`. The text then has a null language. So, after any enrichment retry on a v1.4 run, run `detect-languages` again. Also rebuild the analysis and the card.

The analysis writes `analysis/languages.parquet` with exact label counts for each tag. The card publishes the count of distinct languages, the labeled-text totals, and the top labels.

You can run the standalone stage on a complete run that is analyzed, card-built, or not frozen. In this case, the stage reopens the run as `enriching` and finishes at `enriched`. Afterward, rebuild the analysis, the card, the verification, and the finalization. A snapshot with `status=complete` and `snapshot_status=done` is immutable. The command rejects it before the model loads. The tests use injected fakes and `tmp_path`. They never download GlotLID. They never write production data.

## Run language detection on Grid'5000

Everything is under the data root: `runs/`, `models/`, and the Grid'5000 bundle directories that the command produces.

The repository includes wrappers in `scripts/grid5000/` for short, resumable jobs. They follow the Grid'5000 usage policy. You use the frontend only for checkout, transfer, submission, and monitoring. The detection runs on one reserved GPU node. Each job requests `host=1/gpu=1,walltime=0:30`. It gives the detector a budget of 1,500 seconds. The remaining five minutes are for job cleanup and transfer. The pinned FastText model of GlotLID is CPU-bound. The GPU reservation gives the requested isolated workers and the capacity for parallel jobs. It does not give GPU acceleration for inference.

First, download the pinned public model into the data-root cache. Then prepare one new bundle. The preparation records the repository commit, the source row count and hash, the model revision and hash, and the batch and budget settings. It copies exactly one unfinished public shard and any validated language checkpoint prefix:

```bash
hf download cis-lmu/glotlid model_v3.bin \
  --revision 85cd671 \
  --cache-dir "${OSM_POLY_DATA_DIR:-./data}/models/glotlid"

export OSM_POLY_RUN_DIR="${OSM_POLY_DATA_DIR:-./data}/runs/<run-id>"
export OSM_POLY_BUNDLE_DIR="${OSM_POLY_DATA_DIR:-./data}/grid5000/<bundle-id>"
export OSM_POLY_MODEL_PATH="${OSM_POLY_DATA_DIR:-./data}/models/glotlid/<snapshot>/model_v3.bin"
export OSM_POLY_COMMIT="$(git rev-parse HEAD)"
scripts/grid5000/prepare_language_detection.sh
```

Use `rsync` to copy the checkout and the bundle to the frontend of the selected site. Before the first detection job, bootstrap the locked Linux runtime one time. Point the same policy-aware submit wrapper at `scripts/grid5000/bootstrap_language_runtime.sh`. Wait for that job to finish. Clear its active marker only after you confirm its terminal state. The bootstrap installs the runtime dependencies without the development group. All later detection jobs use that environment offline. Then submit one job with `scripts/grid5000/submit_language_detection.sh`. The wrapper runs `usagepolicycheck -t` before and after `oarsub`. It records the OAR job ID. It refuses to submit while its active marker exists. The wrapper uses the Nancy GPU queue `abaca` by default. To use another site, override it with `GRID5000_QUEUE`. Set `GRID5000_GPUS=1` for each job. Submit different bundles one at a time or in a small staged wave. Do not submit duplicate jobs or speculative jobs. Monitor with `oarstat -u`.

The runner uses a Python module entry point with few dependencies. The default is checkpoint batches of 256 rows. It does not import the extraction-only `osmium` extension on compute nodes. The module `expat/2.7.1` from the site stays loaded for compatibility with the locked runtime.

**WARNING:** Do not run Python, model inference, compilation, or bulk processing on the frontend.

The node runner sets `HF_HUB_OFFLINE=1`. It uses only the staged model and shard. It never fetches website URLs or Hub weights.

After completion, copy the bundle back under the data root. Run `scripts/grid5000/sync_language_detection.sh`. A result that has only checkpoints leaves the canonical shard byte-identical. You can resume it if you prepare a fresh bundle. The command validates the checksums and the schema of completed results before the atomic promotion and the manifest update. Keep the local bundle and the receipt as provenance. Clean the temporary Grid'5000 copies only after you verify the receipt and the checksums. To cancel a job that you do not need, run `oardel <job-id>`. Clear its marker only after you confirm that the job no longer runs.

## Segment sentences on Grid'5000

Sentence segmentation runs only on reserved nodes. The stage consumes the language-complete v1.4 shards. It calls the pinned [SaT](https://huggingface.co/segment-any-text/sat-3l-sm) segmenter (`sat-3l-sm`, revision `137da05`) one time for each website field. It promotes a v1.5 shard. A row with a detected language outside the 85 languages of the segmenter records `unsupported_language`. It does not record sentences. This is about 3% of the extracted texts.

The reserved GPU does the work. The loader moves the pinned encoder to the CUDA device of the node in half precision. It segments 256 texts for each forward batch. It falls back to CPU only when a node has no usable accelerator. The locked CUDA runtime supports compute capability 7.5 and later. So a job must run on a Turing GPU or a later GPU. The Pascal nodes of Nancy fall back to CPU silently. They segment about twenty times slower. Before you submit, limit the reservation with `GRID5000_PROPERTIES`:

```bash
export GRID5000_PROPERTIES="cluster IN ('gres','gruss','grouille','graffiti','grue','grat')"
```

The GlotLID stage is CPU-bound. This stage is a real GPU workload. The segmenter also loads the `facebookAI/xlm-roberta-base` tokenizer. So that Hub cache must exist under `~/.cache/huggingface` on the site before the first offline job.

A segment keeps its own trailing whitespace. It does not keep the line breaks that separated it. So, if you concatenate the sentences of a row, you do not get its extracted text. The untouched text stays in the `*_text` columns.

One bundle carries several shards. Segmentation is fast. A whole reservation of 30 minutes would be spent on the staging of one small country. So `grid5000-prepare-sentences` packs unfinished shards in a stable order, up to `--max-rows`. The node segments them under one shared budget. The command checkpoints each completed batch. If a job uses its whole budget in the middle of a shard, copy it back, synchronize it, and let the next bundle resume it.

The segmentation backend (`wtpsplit` and `torch`) is the optional `sentences` extra. The default image, the Mac checkout, and each run that only extracts have no deep-learning runtime. Only the reserved node installs it:

```bash
export GRID5000_JOB_SCRIPT="$GRID5000_REPO_DIR/scripts/grid5000/bootstrap_sentence_runtime.sh"
scripts/grid5000/submit_language_detection.sh
```

Stage the pinned model one time under the model cache of the data root. Keep only the files that the loader reads (`config.json` and `model.safetensors`). Then prepare a bundle:

```bash
export OSM_POLY_RUN_DIR="${OSM_POLY_DATA_DIR:-./data}/runs/<run-id>"
export OSM_POLY_BUNDLE_DIR="${OSM_POLY_DATA_DIR:-./data}/grid5000-sentences/<bundle-id>"
export OSM_POLY_MODEL_DIR="${OSM_POLY_DATA_DIR:-./data}/models/sat/sat-3l-sm-min"
export OSM_POLY_MODEL_REVISION='137da054051ad9f1eac42025f758db4ac9f22535'
export OSM_POLY_COMMIT="$(git rev-parse HEAD)"
scripts/grid5000/prepare_sentence_segmentation.sh
```

Copy the bundle to the job directory. Submit exactly one job. Use the same policy-aware submission wrapper with the sentence job script:

```bash
export GRID5000_JOB_SCRIPT="$GRID5000_REPO_DIR/scripts/grid5000/run_sentence_segmentation.sh"
scripts/grid5000/submit_language_detection.sh
```

The model directory is immutable and the same for each bundle. So you stage it one time on the site. The command links it into each bundle. It does not transfer it for each job. The runner still verifies its digest against the manifest before it loads the model. After the job reaches a terminal state, copy the bundle back and synchronize it:

```bash
export OSM_POLY_BUNDLE_DIR="${OSM_POLY_DATA_DIR:-./data}/grid5000-sentences/<bundle-id>"
scripts/grid5000/sync_sentence_segmentation.sh
```

The synchronization validates the schema, the row count, and the digest of each completed v1.5 shard. Then it installs the shard in an atomic step. It installs a validated checkpoint for a paused shard. It updates the source manifest. It records a receipt under `manifests/grid5000-sentences/`. After each shard has sentences, rebuild the analysis, the card, the verification, and the finalization. Then publish.

## Dry run and apply

Without `--apply`, `run-all` computes and verifies local artifacts. It does not upload to Hugging Face. `publish` and `publish-plan` are also read-only by default. This is the review path. Inspect the run, the card, the map, and the plan before you give the approval for publication.

With `--apply`, the command resolves the credentials from `HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`, or the local Hugging Face credential store. The workflow reconciles the local upload checkpoint with the remote polygon hashes. It uploads an enriched shard and a refreshed card together. It records the acknowledgement before it continues. At the end, it uploads only the files that the completion receipt binds. A missing credential, a changed source inventory, or a failed verification stops the run before the related upload.

## What is local and what is public

The run directory is local until an apply-mode publication succeeds. It contains the public polygon shards. It also contains the comparison, rejection, analysis, card, map, and manifest artifacts. `manifests/uploaded_polygons.json` is operational resume state. The completion receipt excludes it. The receipt also excludes the staging files and the spill files.

The public Hugging Face dataset is the subset that the verified receipt selects. It has `polygons/*.parquet`, the receipt-bound analysis and supporting artifacts, the generated card, and the map when it exists. A local run can contain newer or incomplete work compared with the public dataset. Do not say that local-only files are published until the apply-mode upload is complete and you checked it.

## Safe publication checklist

1. Confirm that the source root is unchanged and that the run is complete.
2. Run `verify-results --run-dir '<run-dir>'`. Review the generated card.
3. Run `publish-plan --run-dir '<run-dir>'`. Inspect its artifact list.
4. Get a separate approval. Authenticate with `hf auth login`. Add `--apply` to `publish` (or to the reviewed `run-all` command).

The publisher runs the verification again immediately before any upload. It refuses partial runs and tampered runs. In Docker, keep the source mount read-only. Pass a token through the environment only. Do not put credentials in a file that you copy into an image. Do not put them in a command-line argument.

## Freeze the current data without more retries

You can decide that the current extracted and text results are the final snapshot. In this case, do not resume `run-all`. It retries unresolved URL outcomes on purpose. Put a reviewed `snapshot_status: done` marker in `manifests/run.json`. Then run:

```bash
uv run --locked osm-polygon-website-tag finalize-snapshot \
  --run-dir '<run-dir>'
```

This command reads only the existing shards. It refuses unfinished `pending` text rows. It keeps the recorded fetch failures and the deterministic URL rejections. Then it builds the analysis tables, the card, and the map. It verifies each artifact and writes the completion receipt. It makes no PBF reads, no URL requests, and no enrichment retries.

## Publish the metrics dashboard

The dataset card links to the public [Trackio Space](https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics). Its metrics come from the same `CardStats` projection as the card. The command publishes them only from a run with `status=complete`, a valid completion receipt, and a fresh successful verification. Preview them without a network call:

```bash
uv run --locked osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>'
```

After you review the output, the explicit remote action is:

```bash
uv run --with trackio osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>' \
  --space-id 'NoeFlandre/osm-polygon-website-tag-metrics' \
  --apply
```

The optional Trackio client creates or refreshes a public, read-only static Space when necessary. It logs one run that the receipt names. It sends only aggregate numeric dataset metrics and a SHA-256 snapshot identifier. It sends no website text and no credential to the dashboard. This command is separate from `run-all`. So the normal PBF pipeline is not affected if the dashboard is not available.
