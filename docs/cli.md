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
| `publish-plan` | Show the receipt-bound files that would be uploaded. |
| `publish` | Dry-run publication, or upload with explicit `--apply`. |
| `create-repo` | Explicitly create a public Hugging Face dataset repository. |
| `card-stats` | Recompute and print card statistics for a run. |
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

For a manually staged run, the phase sequence is:

```text
init -> extract -> run-all-owned enrichment -> analyze-results
     -> build-card -> verify-results -> finalize-run -> publish
```

`extract` also accepts `--area-workers` and `--max-in-flight-areas`. The
enrichment phase is intentionally owned by `run-all`, because it coordinates
the URL cache, retryable statuses, durable batch checkpoints, and per-source
upload acknowledgements.

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
