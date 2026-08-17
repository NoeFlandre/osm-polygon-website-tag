# OSM Polygon Website Tag

This project builds a reproducible dataset of OpenStreetMap (OSM) polygons
with a non-empty `website` or `contact:website` tag. It reads closed ways and
supported polygon relations from immutable PBF inputs, assembles their
geometry with libosmium, preserves the original tags, extracts website text,
and writes a versioned Parquet shard for each source PBF.

## Start here

1. Follow [Getting started](setup.md) to install the locked environment.
2. Use the [CLI reference](cli.md) for copyable commands and options.
3. Read [Operations and resume](operations.md) before a long run or upload.
4. See [Architecture](architecture.md) for module and artifact boundaries.
5. See [Data and remotes](data-and-remotes.md) for storage and publication.

## Local versus public output

Local run artifacts live under the writable `--output-root` and include
Parquet shards, analysis tables, the generated card and map, manifests, and a
completion receipt. They are not committed to this repository and are not
public merely because a run completed locally.

The [public Hugging Face dataset](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag)
is updated only by explicit `--apply`. A final `publish --apply` upload of a
complete run is selected by its verified completion receipt; `run-all --apply`
also uploads checkpointed shard/card/map progress before finalization. Its
generated card is the source of truth for the published snapshot. A local run
may be newer, partial, or unreviewed.

## Safety model

The production PBF directory is a read-only input. Run artifacts go to a
separate output root, normally on the Seagate data volume. `run-all` records
the source inventory, checks it again on resume, and keeps successful
enrichment and upload checkpoints. `publish` is a dry run unless `--apply` is
present; both commands re-verify before publication.

The [GitHub repository](https://github.com/NoeFlandre/osm-polygon-website-tag)
contains the source code, tests, and Pages workflow. Current row and text
totals belong to the generated public card, not to this landing page.

The current software release is [v0.1.0](https://github.com/NoeFlandre/osm-polygon-website-tag/releases/tag/v0.1.0).
