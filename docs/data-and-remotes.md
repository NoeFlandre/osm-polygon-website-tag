# Data and remotes

Where data lives on disk, and how it gets to Hugging Face.

## Run-artifact root

Code stays on the MacBook. Generated artifacts live on the Seagate under
`/Volumes/Seagate M3/projects/osm-polygon-website-tag-data`; override it with
`OSM_POLY_DATA_DIR`. Production commands use an explicit `--output-root`.

## Immutable PBF sources

Production PBFs live at:

```
/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw
```

The pipeline **never writes** to this directory. The safety module
(`osm_polygon_website_tag.safety`) refuses any output path that is equal
to or contained by the required `--source-root`. PBFs are read-only
inputs: the pipeline never hashes, copies, moves, renames, or modifies them.

## Output root

The pipeline accepts an explicit `--output-root` outside the source root.
Each run owns this layout:

```
<output-root>/<run-id>/
  polygons/<source-stem>.parquet
  analysis_observations/<source-stem>.parquet
  rejections/<source-stem>.parquet
  analysis/*.parquet
  manifests/
  README.md
  dataset.yaml
```

Run-owned staging is excluded from the completion receipt and publication.
Publication uses only receipt-bound paths.

## GitHub remote

```
https://github.com/NoeFlandre/osm-polygon-website-tag.git
```

Push flow (typical):

```bash
git status
git diff --staged   # review before committing
git add <files>
git commit -m "<message>"
git push origin main
```

## Hugging Face dataset remote

```
https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag
```

Use the `publish` subcommand, which is **dry-run by default** and
verifies the local run before any upload.

The resumable production command is documented in the root README. With
`run-all --apply`, each polygon shard is safely enriched from both website
tags, the cumulative card is recomputed from Parquets, and the shard plus card
are uploaded together; a local acknowledgement is then written atomically
before the next PBF begins. The final analysis, card, manifests, and completion
receipt are uploaded only after the entire inventory verifies. Stopping with
`Ctrl-C` and repeating the same command resumes without reprocessing exact
completed bundles or successful URLs. This includes bundles created by the old
extract-all workflow: they are enriched and uploaded without rereading their
PBFs. Legacy shards are likewise enriched without rereading PBFs; failed URLs
retry.

Public schema v1.2 shards are migrated locally to v1.3 by column projection.
The migration preserves extracted text and row order, performs no PBF or
network work, and causes only the changed shard plus recomputed card to upload.
An acknowledged v1.3 shard is skipped on the next resume.

```bash
# One-time
hf auth login                                  # paste a write token from
                                               # https://huggingface.co/settings/tokens

# After artifacts are produced in <output-root>/<run_id>
uv run osm-polygon-website-tag verify-results \
  --run-dir '<output-root>/<run_id>'

uv run osm-polygon-website-tag publish \
  --run-dir '<output-root>/<run_id>'            # dry-run

uv run osm-polygon-website-tag publish \
  --run-dir '<output-root>/<run_id>' \
  --apply                                      # real upload, approval-gated
```

The CLI never accepts an HF token as a flag. The token is read from
`HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`, or the local Hugging Face
credential store via `hf auth login`. `publish` re-runs
`verify-results` before any upload; a partial or tampered run is
rejected before any HTTP traffic is initiated.

## Why split code and data

- **Code in git, data on disk** keeps the repo cloneable and lightweight.
- The external drive provides the storage needed for planet-scale OSM data,
  which would otherwise bloat history.
- Treating `raw/` as immutable mirrors the way OSM providers serve data:
  every extraction produces a new run-owned directory.
