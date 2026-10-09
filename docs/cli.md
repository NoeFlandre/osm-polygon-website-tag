# CLI reference

The installed command is `osm-polygon-website-tag`. To show the same list that Typer shows, run `uv run --locked osm-polygon-website-tag --help`. `run-all` is the normal entry point. The phase commands help with development, recovery, and inspection of an existing run.

## Global options

Put them before the command. For example: `osm-polygon-website-tag -q run-all ...`.

| Option | Effect |
| --- | --- |
| `--version` | Prints the package version and exits with code 0. |
| `-v`, `--verbose` | Logs on stderr. `-v` shows INFO. `-vv` shows DEBUG (for example, whether a custom data root or the default data root is in use). |
| `-q`, `--quiet` | Shows only errors on stderr and no progress output. stdout keeps the JSON result. |
| `--debug` | Shows full tracebacks, not one-line errors (also `OSM_PWT_DEBUG=1`). |

You cannot combine `-v` and `-q`. Logs never go to stdout.

## Exit codes and errors

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | A check failed (`verify-results`, `finalize-*`). |
| `2` | Usage error: unknown command or option, missing value. |
| `3` | Invalid input or state: a bad value, a missing or unreadable file, a corrupt manifest. |
| `4` | Remote failure: a Hugging Face Hub HTTP or authentication error. |
| `5` | A required optional package is missing (`publish-trackio` without `trackio`). |
| `130` | Interrupted with Ctrl-C. |

An error prints one `error: ...` line on stderr. It never prints a traceback. To get the full traceback, pass `--debug` before the command (`osm-polygon-website-tag --debug run-all ...`). Or set `OSM_PWT_DEBUG=1`.

## Commands

| Command | Purpose |
| --- | --- |
| `init` | Creates a run and records its exact expected PBF inventory. |
| `extract` | Extracts one inventoried PBF into the run-owned shards. |
| `analyze-results` | Builds external-memory analysis tables after enrichment. |
| `build-card` | Computes `README.md`, `dataset.yaml`, and `stats.json` again from the artifacts. |
| `verify-results` | Checks schemas, counts, hashes, and required artifacts. It does not change the run. |
| `refresh-card` | Rebuilds the local map and card. Refreshes the completion receipt of an older run. |
| `finalize-run` | Verifies a card-built run and writes its completion receipt. |
| `finalize-snapshot` | Finishes an explicitly frozen snapshot. It does not retry website enrichment. |
| `publish-plan` | Shows the receipt-bound files that the command would upload. |
| `publish` | Makes a dry-run publication. Uploads only with explicit `--apply`. |
| `release-stats` | Computes again and publishes only the dataset card and the statistics report. |
| `create-repo` | Reports if a Hugging Face dataset repository exists. `--apply` creates it. |
| `card-stats` | Computes again and prints the card statistics of a run. |
| `geometry-stats` | Computes again and prints the polygon geometry statistics of a run. |
| `publish-trackio` | Previews or publishes the metrics of one finalized snapshot to the public Trackio Space. |
| `detect-languages` | Adds resumable GlotLID language results to public polygon shards. |
| `grid5000-prepare` | Stages one unfinished shard, its checkpoint, and the pinned model for an offline job. |
| `grid5000-run` | Detects languages in one staged bundle on a reserved node without network access. |
| `grid5000-sync` | Validates one paused or completed bundle and synchronizes it into the canonical run. |
| `segment-sentences` | Segments website text into sentences for each language-complete shard. |
| `grid5000-prepare-sentences` | Stages one offline Grid'5000 sentence-segmentation bundle. |
| `grid5000-run-sentences` | Segments one staged sentence bundle on a reserved node without network access. |
| `grid5000-sync-sentences` | Validates one sentence bundle receipt and synchronizes it into the canonical run. |
| `run-all` | Discovers, extracts, enriches, analyzes, verifies, and resumes a complete inventory. |

Each `--run-dir` value points to an existing run directory. The commands that take `--source-root` and `--output-root` need these paths explicitly. This keeps the immutable inputs and the generated output separate.

## Recommended workflow

For a local run that does not publish:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root '/path/to/read-only/pbf-root' \
  --output-root '/path/to/writable/runs' \
  --run-id 'website-v1'
```

Publication is a dry run by default. Without `--apply`, the command still does local extraction, enrichment, analysis, and verification. It does not upload to Hugging Face. To resume, repeat the same command. Read [Operations and resume](operations.md) for the checkpoint rules and the source-integrity rules. `--repo-id` changes only the Hugging Face destination that a later upload in apply mode uses.

`run-all` accepts these optional controls:

| Option | Default | Meaning |
| --- | --- | --- |
| `--apply` | off | Uploads each completed shard and the final receipt-bound bundle. |
| `--ensure-repo` | off | Creates the dataset repository when it is necessary. Valid only with `--apply`. |
| `--area-workers` | 4 | Bounded geometry workers for each PBF. |
| `--max-in-flight-areas` | 32 | Maximum number of queued geometry payloads for each PBF. |
| `--fetch-workers` | 8 | Bounded concurrent URL fetch workers for each enrichment batch. |
| `--host-concurrency` | 2 | Maximum number of simultaneous requests to one website host (`OSM_PWT_HOST_CONCURRENCY`). |
| `--host-delay-seconds` | 0.2 | Minimum seconds between request starts to one host (`OSM_PWT_HOST_DELAY_SECONDS`). |
| `--detect-languages` | off | Loads the pinned GlotLID model and adds the schema-v1.4 language fields. |

A `429` or `503` reply can have a `Retry-After` of 30 seconds or less. In this case, the host pauses and the fetcher retries one time. A longer `Retry-After`, or no `Retry-After`, stays a retryable `http_429` / `http_503`.

For a run that you stage by hand, the sequence of phases is:

```text
init -> extract -> run-all-owned enrichment -> analyze-results
     -> build-card -> verify-results -> finalize-run -> publish
```

Language detection is optional. To run it after text enrichment, add `--detect-languages` to `run-all`. Or run it separately on an enriched run:

```bash
uv run --locked osm-polygon-website-tag detect-languages \
  --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/<run-id>"
```

The standalone command loads one pinned GlotLID V3 model from the data-root cache (`<data root>/models/glotlid`). It processes the public shards in sorted order. After all shard promotions succeed, it changes the run from `enriching` to `enriched`. If the run was already analyzed or card-built, run `analyze-results`, `build-card`, `verify-results`, and `finalize-run` again afterward. The command never loads the model when no language shard is unfinished. The command rejects a frozen snapshot before it opens the model cache.

For Grid'5000, three bundle commands keep the staging, the execution, and the synchronization explicit:

```bash
uv run --locked osm-polygon-website-tag grid5000-prepare \
  --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/<run-id>" \
  --bundle-dir "${OSM_POLY_DATA_DIR:-./data}/grid5000/<bundle-id>" \
  --model-path "${OSM_POLY_DATA_DIR:-./data}/models/glotlid/<snapshot>/model_v3.bin" \
  --commit "$(git rev-parse HEAD)"

uv run --locked --offline osm-polygon-website-tag grid5000-run \
  --bundle-dir '/path/to/staged/bundle' \
  --time-budget-seconds 1500 --batch-rows 256

uv run --locked osm-polygon-website-tag grid5000-sync \
  --bundle-dir "${OSM_POLY_DATA_DIR:-./data}/grid5000/<bundle-id>" \
  --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/<run-id>"
```

`grid5000-prepare` and `grid5000-sync` reject paths outside the data root (`OSM_POLY_DATA_DIR`, default `./data`). `grid5000-run` accepts only a staged bundle. It never calls Hugging Face or the website-fetching code. The shell wrapper for the reserved node calls a module entry point with few dependencies. This way, it does not import native libraries that only extraction needs. The default is checkpoint batches of 256 rows. The shell wrappers add the OAR resource request and the policy checks. Read [Operations and resume](operations.md).

The two bounded-run options take these types:

- `--time-budget-seconds` takes whole seconds (`<int>`) on `grid5000-prepare` and `grid5000-prepare-sentences`, because the bundle stores the budget as an integer.
- `--time-budget-seconds` takes fractional seconds (`<float>`) on `grid5000-run`, `grid5000-run-sentences`, `detect-languages`, and `segment-sentences`.
- `--batch-rows` takes whole rows (`<int>`) on every command. On `grid5000-run` and `grid5000-run-sentences`, the value is an optional override.

Each Grid'5000 budget must be in (0, 1500] seconds.

`extract` also accepts `--area-workers` and `--max-in-flight-areas`. `run-all` owns the enrichment phase on purpose. It coordinates the URL cache, the retryable statuses, the durable batch checkpoints, and the upload acknowledgements for each source.

The owner can decide to stop the retries of URL failures. In this case, do these steps:

1. Set `snapshot_status` to `done` in the run metadata. Use the reviewed project workflow.
2. Run `finalize-snapshot`.

`finalize-snapshot` reuses the existing Parquet shards. It requires that no text status is still `pending`. It builds the analysis, card, and map artifacts. It verifies them and writes the receipt. It keeps the recorded outcomes `fetch_error`, `empty`, `unsafe_url`, and the other outcomes. It never calls the enrichment stages or the web-fetch stages. After the receipt exists, a resume of `run-all` for the same run does nothing, on purpose. The command does not reopen the frozen snapshot. It does not retry or upload.

## Publication commands

Inspect a complete run. These commands make no network writes:

```bash
uv run --locked osm-polygon-website-tag verify-results --run-dir '<run-dir>'
uv run --locked osm-polygon-website-tag publish-plan \
  --run-dir '<run-dir>' \
  --repo-id 'NoeFlandre/osm-polygon-website-tag'
uv run --locked osm-polygon-website-tag publish \
  --run-dir '<run-dir>' \
  --repo-id 'NoeFlandre/osm-polygon-website-tag'
```

`publish` is read-only, unless `--apply` is present. Apply mode needs a Hugging Face credential. Supply it through the environment or through the local `hf auth login`. The CLI never accepts a token flag. `create-repo` is separate. Like `publish`, it only reports what it would do, until you add `--apply`. The CLI rejects `--ensure-repo`, unless `run-all` is also in apply mode.

### Release the card and the statistics report

`release-stats` is the wrapper for the statistics release. It verifies the complete published run. It computes `README.md` and `stats.json` again from each published row. It uploads these files, with the regenerated completion receipt, to the exact dataset as one metadata commit. It never touches the polygon shards or unrelated Hub files.

```bash
# 1. Dry run: verify, recompute, and print the exact plan. No network writes.
uv run --locked osm-polygon-website-tag release-stats \
  --run-dir '<run-dir>' \
  --confirm-repo 'NoeFlandre/osm-polygon-website-tag'

# 2. Publish and verify the remote revision.
uv run --locked osm-polygon-website-tag release-stats \
  --run-dir '<run-dir>' \
  --confirm-repo 'NoeFlandre/osm-polygon-website-tag' \
  --apply
```

The release is bound to the canonical `NoeFlandre/osm-polygon-website-tag` dataset. The command refuses any `--repo-id` override. `--confirm-repo` must match the canonical value before any network call. The command needs a complete run and its completion receipt. The JSON report records these items: the deterministic source and data manifest digest, the verified remote revision, each released file with its SHA-256 and size, and whether apply mode uploaded or found an exact remote no-op. The command writes `stats.json` again only when its bytes change. So a second apply over an unchanged remote is a no-op.

Equivalent recipes: `just release-stats-dry-run '<run-dir>'` and `just release-stats '<run-dir>'`.

## Trackio metrics dashboard

The generated dataset card links to the public [Trackio dashboard](https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics). The metrics come from the same finalized Parquets as the card. The command is a dry run by default. It does not need Trackio to be installed:

```bash
uv run --locked osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>'
```

First, review the JSON metrics separately. Then install the optional Trackio client for the explicit remote action. Run the command again with `--apply`:

```bash
uv run --with trackio osm-polygon-website-tag publish-trackio \
  --run-dir '<complete-run-dir>' \
  --space-id 'NoeFlandre/osm-polygon-website-tag-metrics' \
  --apply
```

In apply mode, the command creates or refreshes a public, read-only static Space if necessary. It uses the static SDK of Trackio. It logs one stable run that comes from the receipt. It sends only numeric dataset metrics and a non-sensitive dataset revision digest. The credentials come from the normal Hugging Face environment or local credential resolution of Trackio. No token flag exists.
