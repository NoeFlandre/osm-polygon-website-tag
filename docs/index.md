# OSM Polygon Website Tag

This project builds a reproducible public dataset of OpenStreetMap polygons
whose `website` or `contact:website` tag is non-empty. It reads closed ways
and polygon relations from immutable PBF inputs, assembles their geometry with
libosmium, preserves the original OSM tags, and writes one versioned Parquet
shard per source PBF.

## Start here

1. Follow [Getting started](setup.md) to install the locked environment.
2. Read [Architecture](architecture.md) for the extraction, enrichment, and
   verification boundaries.
3. Read [Data and remotes](data-and-remotes.md) before working with the
   Seagate-backed PBFs or publishing to Hugging Face.

The generated Hugging Face dataset card is the source of truth for current
row, text, word, and hostname totals. It is rebuilt from the Parquet artifacts
after each incremental upload; detailed overlap and per-source analyses stay
in the published `analysis/` files.

## Public outputs

- [Hugging Face dataset](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag)
  — public Parquet shards, analysis artifacts, and the generated card.
- [GitHub repository](https://github.com/NoeFlandre/osm-polygon-website-tag)
  — source code, tests, and the publication workflow.

## Safety model

The production PBF directory is read-only input. Run artifacts are written to
the separate Seagate data root, and every resumable command verifies source
fingerprints and local manifests before continuing. Publication is explicit,
receipt-bound, and dry-run by default.
