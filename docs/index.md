# OSM Polygon Website Tag

This project builds a reproducible dataset of OpenStreetMap (OSM) polygons. Each polygon has a non-empty `website` or `contact:website` tag. The pipeline reads closed ways and supported polygon relations from immutable PBF inputs. It assembles their geometry with libosmium. It keeps the original tags and extracts the website text. It writes one versioned Parquet shard for each source PBF.

## Start here

1. Follow [Getting started](setup.md) to install the locked environment.
2. Use the [CLI reference](cli.md) for commands that you can copy, and for options.
3. Read [Operations and resume](operations.md) before a long run or an upload.
4. Read [Architecture](architecture.md) for the boundaries of modules and artifacts.
5. Read [Data and remotes](data-and-remotes.md) for storage and publication.
6. Read the [Glossary](glossary.md) for the technical names.

## Local output and public output

The local run artifacts are in the writable `--output-root`. They include Parquet shards, analysis tables, the generated card and map, manifests, and a completion receipt. They are not committed to this repository. A run that completes locally is not public.

The [public Hugging Face dataset](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag) changes only when you use an explicit `--apply`. The verified completion receipt selects the final `publish --apply` upload of a complete run. `run-all --apply` also uploads the checkpointed shard, card and map progress before the finalization. The generated card is the source of truth for the published snapshot. A local run can be newer, partial, or not reviewed.

## Safety model

The production PBF directory is a read-only input. The run artifacts go to a separate output root. Normally, this root is under the data root (`OSM_POLY_DATA_DIR`). `run-all` records the source inventory. On resume, it checks the inventory again. It keeps the successful enrichment checkpoints and upload checkpoints. `publish` is a dry run, unless `--apply` is present. Both commands verify again before publication.

The [GitHub repository](https://github.com/NoeFlandre/osm-polygon-website-tag) contains the source code, the tests, and the Pages workflow. The generated public card holds the current totals of rows and text. This landing page does not hold them.

The current software release is [v0.1.0](https://github.com/NoeFlandre/osm-polygon-website-tag/releases/tag/v0.1.0).
