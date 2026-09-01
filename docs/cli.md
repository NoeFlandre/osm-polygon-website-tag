# CLI reference

The installed command is `osm-polygon-website-tag`. Run
`uv run --locked osm-polygon-website-tag --help` for the same list shown by
Typer. `run-all` is the normal entry point; the phase commands are useful for
development, recovery, and inspection of an existing run.

## Commands

| Command | Purpose |
| --- | --- |
| `init` | Create a run and record its exact expected PBF inventory. |
| `extract` | Extract one inventoried PBF into the run-owned shards. |
| `analyze-results` | Build external-memory analysis tables after enrichment. |
| `build-card` | Recompute `README.md` and `dataset.yaml` from the artifacts. |
| `verify-results` | Check schemas, counts, hashes, and required artifacts without changing the run. |
| `refresh-card` | Rebuild the local map/card and refresh the completion receipt for an older run. |
| `finalize-run` | Verify a card-built run and write its completion receipt. |
| `finalize-snapshot` | Finish an explicitly frozen snapshot without retrying website enrichment. |
| `publish-plan` | Show the receipt-bound files that would be uploaded. |
| `publish` | Dry-run publication, or upload with explicit `--apply`. |
| `create-repo` | Explicitly create a public Hugging Face dataset repository. |
| `card-stats` | Recompute and print card statistics for a run. |
| `publish-trackio` | Preview or publish metrics for one finalized snapshot to the public Trackio Space. |
| `detect-languages` | Add resumable GlotLID language results to public polygon shards. |
| `grid5000-prepare` | Stage one unfinished shard, checkpoint, and pinned model for an offline job. |
| `grid5000-run` | Detect one staged bundle on a reserved node without network access. |
| `grid5000-sync` | Validate and synchronize one paused or completed bundle into the canonical run. |
| `run-all` | Discover, extract, enrich, analyze, verify, and resume a complete inventory. |

All `--run-dir` values point to an existing run directory. The commands that
take `--source-root` and `--output-root` require those paths explicitly; this
keeps immutable inputs and generated output separate.

## Recommended workflow

For a local, non-publishing run:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root '/path/to/read-only/pbf-root' \
  --output-root '/path/to/writable/runs' \
  --run-id 'website-v1'
```

Publication is a dry run by default: without `--apply`, the command still
performs local extraction, enrichment, analysis, and verification but does not
upload to Hugging Face. Repeat the same command to resume; see
[Operations and resume](operations.md) for the checkpoint and source-integrity
rules. `--repo-id` only changes the Hugging Face destination used by a later
apply-mode upload.

`run-all` accepts these optional controls:

| Option | Default | Meaning |
| --- | --- | --- |
| `--apply` | off | Upload each completed shard and the final receipt-bound bundle. |
| `--ensure-repo` | off | Create the dataset repository when needed; valid only with `--apply`. |
| `--area-workers` | 4 | Bounded geometry workers per PBF. |
| `--max-in-flight-areas` | 32 | Maximum queued geometry payloads per PBF. |
| `--fetch-workers` | 8 | Bounded concurrent URL fetch workers per enrichment batch. |
| `--detect-languages` | off | Load the pinned GlotLID model and add schema-v1.4 language fields. |

For a manually staged run, the phase sequence is:

```text
init -> extract -> run-all-owned enrichment -> analyze-results
     -> build-card -> verify-results -> finalize-run -> publish
```

Language detection is optional. Add `--detect-languages` to `run-all` to run
it after text enrichment, or run it separately on an enriched run:

```bash
uv run --locked osm-polygon-website-tag detect-languages \
  --run-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>'
```

The standalone command loads one pinned GlotLID V3 model from the Seagate
cache, processes public shards in sorted order, and changes the run from
`enriching` to `enriched` after all shard promotions succeed. If the run was
already analyzed or card-built, rerun `analyze-results`, `build-card`,
`verify-results`, and `finalize-run` afterward. The model is never loaded when
there are no unfinished language shards. A frozen snapshot is rejected before
the model cache is opened.

For Grid'5000, the three bundle commands keep staging, execution, and
synchronization explicit:

```bash
uv run --locked osm-polygon-website-tag grid5000-prepare \
  --run-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>' \
  --bundle-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/grid5000/<bundle-id>' \
  --model-path '/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid/<snapshot>/model_v3.bin' \
  --commit "$(git rev-parse HEAD)"

uv run --locked --offline osm-polygon-website-tag grid5000-run \
  --bundle-dir '/path/to/staged/bundle' \
  --time-budget-seconds 1500 --batch-rows 256

uv run --locked osm-polygon-website-tag grid5000-sync \
  --bundle-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/grid5000/<bundle-id>' \
  --run-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>'
```

`grid5000-prepare` and `grid5000-sync` reject paths outside the Seagate data
root. `grid5000-run` accepts only a staged bundle and never calls Hugging Face
or the website-fetching code. The reserved-node shell wrapper invokes a
dependency-light module entry point so it does not import extraction-only
native libraries. It defaults to 256-row checkpoint batches. The shell
wrappers add the OAR resource request and policy checks; see [Operations and
resume](operations.md).

`extract` also accepts `--area-workers` and `--max-in-flight-areas`. The
enrichment phase is intentionally owned by `run-all`, because it coordinates
the URL cache, retryable statuses, durable batch checkpoints, and per-source
upload acknowledgements.

If the owner has decided to stop retrying URL failures, first set
`snapshot_status` to `done` in the run metadata through the reviewed project
workflow, then use `finalize-snapshot`. It reuses the existing Parquet shards,
requires that no text status is still `pending`, builds analysis/card/map
artifacts, verifies them, and writes the receipt. It preserves recorded
`fetch_error`, `empty`, `unsafe_url`, and other outcomes; it never calls the
enrichment or web-fetch stages.
Once that receipt exists, resuming `run-all` for the same run is an intentional
no-op: the frozen snapshot is not reopened and no retry or upload is attempted.

## Publication commands

Inspect a complete run without network writes:

```bash
uv run --locked osm-polygon-website-tag verify-results --run-dir '<run-dir>'
uv run --locked osm-polygon-website-tag publish-plan \
  --run-dir '<run-dir>' \
  --repo-id 'NoeFlandre/osm-polygon-website-tag'
uv run --locked osm-polygon-website-tag publish \
  --run-dir '<run-dir>' \
  --repo-id 'NoeFlandre/osm-polygon-website-tag'
```

`publish` is read-only unless `--apply` is present. Apply mode requires a
Hugging Face credential supplied through the environment or local `hf auth
login`; the CLI never accepts a token flag. `create-repo` is separate and
explicit, and `--ensure-repo` is rejected unless `run-all` is also in apply
mode.

## Trackio metrics dashboard

The generated dataset card links to the public
[Trackio dashboard](https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics).
Metrics are derived from the same finalized Parquets as the card. The command
is dry-run by default and does not require Trackio to be installed:

```bash
uv run --locked osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>'
```

After separately reviewing the JSON metrics, install the optional Trackio
client for the explicit remote action and rerun with `--apply`:

```bash
uv run --with trackio osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>' \
  --space-id 'NoeFlandre/osm-polygon-website-tag-metrics' \
  --apply
```

Apply mode creates or refreshes a public, read-only static Space if needed,
using Trackio's static SDK. It logs one stable receipt-derived run and sends
only numeric dataset metrics plus a non-sensitive dataset revision digest.
Credentials come from Trackio's normal Hugging Face environment/local
credential resolution; no token flag exists.
