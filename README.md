# OSM Polygon Website Tag

![OSM Polygon Website Tag](assets/hero.png)

This project is a reproducible pipeline and a public dataset. The dataset contains OpenStreetMap (OSM) polygons that have a non-empty `website` or `contact:website` tag. The current snapshot is complete and published. The dataset card is the source of truth for its totals.

[Hugging Face dataset card](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag) ·
[Trackio metrics](https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics) ·
[Documentation](https://noeflandre.github.io/osm-polygon-website-tag/) ·
[Latest release: v0.1.0](https://github.com/NoeFlandre/osm-polygon-website-tag/releases/tag/v0.1.0) ·
[Citation](CITATION.cff)

## What this project produces

The pipeline reads immutable OSM PBF extracts. It selects polygon geometries from closed ways and from supported polygon relations. Then it does these tasks:

- It keeps the original OSM tags and the provenance.
- It stores one versioned Parquet shard for each source PBF.
- It extracts the main text from `website` and from `contact:website` separately, with [Trafilatura](https://trafilatura.readthedocs.io/).
- It records the text status, the word counts, the geometry, and the source metadata.
- It generates a dataset card from the artifacts and a text-only H3 geographic map.
- It reports deterministic polygon geometry statistics (surface, shape, extent) for each published row in `stats.json` and in a block of the dataset card.

The default public polygon schema has a version (`v1.3`). It is documented in [`contracts/polygon_schema.py`](src/osm_polygon_website_tag/contracts/polygon_schema.py). The opt-in `--detect-languages` stage adds GlotLID top-1 labels and probabilities as schema `v1.4`. The opt-in sentence stage adds SaT sentences, their counts, and a segmentation status for each field as schema `v1.5` (see [Outputs](#outputs)). Both stages keep their model caches under the data root (see [Configuration](#configuration)). The pipeline keeps the full extracted text and does not truncate it.

The [dataset card](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag) holds the current totals of rows, text, and words. This README does not repeat them.

Website content is third-party material. The pipeline applies URL safety checks before it fetches the content. The dataset does not give additional rights to that content.

See also the [glossary](docs/glossary.md).

## Install

The project uses Python 3.12, [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and [`just`](https://just.systems/man/en/packages.html). Both links cover Linux, macOS and Windows (on macOS: `brew install uv just`). Start from a fresh clone:

```bash
git clone https://github.com/NoeFlandre/osm-polygon-website-tag
cd osm-polygon-website-tag
just sync
just check
```

## Usage

Run the pipeline with an immutable, read-only PBF directory and a separate writable output directory:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root /path/to/read-only/pbf-root \
  --output-root /path/to/writable/runs \
  --run-id website-v1
```

You can safely press `Ctrl-C`. To resume, repeat the same command. The run resumes the verified shards, the URL-cache results, the completed enrichment batches, and the acknowledged uploads. The pipeline never changes the PBF inputs. It processes the PBF files one after the other. Extraction and fetching use bounded workers. For the checkpoint and freeze rules, read [Operations and resume](docs/operations.md).

Language detection is opt-in. You can include it in the same resumable run:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root /path/to/read-only/pbf-root \
  --output-root "${OSM_POLY_DATA_DIR:-./data}/runs" \
  --run-id website-v1 \
  --detect-languages
```

For a run that is already enriched, use the standalone stage:

```bash
uv run --locked osm-polygon-website-tag detect-languages \
  --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/website-v1"
```

The command downloads the pinned [GlotLID model](https://huggingface.co/cis-lmu/glotlid) to `$OSM_POLY_DATA_DIR/models/glotlid/`. Language runs must be under the data root. The default `run-all` path does not load or download the model.

You can also run language detection in short, offline Grid'5000 jobs. Use the wrappers in `scripts/grid5000/`. Read [Operations and resume](docs/operations.md#run-language-detection-on-grid5000).

Sentence segmentation runs in the same way. Each job processes one bundle of several shards on a reserved GPU. Read [Segment sentences on Grid'5000](docs/operations.md#segment-sentences-on-grid5000). The published snapshot was segmented entirely on reserved Grid'5000 nodes.

For a reproducible container workflow, read [Getting started](docs/setup.md#docker-workflow).

## Configuration

The pipeline reads settings from the environment or from a `.env` file (see [`.env.example`](.env.example)):

| Variable | Meaning | Default |
| --- | --- | --- |
| `OSM_POLY_DATA_DIR` | Data root for runs, model caches and Grid'5000 bundles | `./data` |
| `HF_DATASET_REPO` | Default target for `publish`, `publish-plan`, `run-all` and Trackio metadata; `release-stats` remains canonical-only | `NoeFlandre/osm-polygon-website-tag` |
| `HF_TOKEN` | Hugging Face token for publishing (or use `hf auth login`) | none |

## Outputs

Each run owns `<output-root>/<run-id>/`:

```text
polygons/<source-stem>.parquet     one polygon shard per source PBF
rejections/, analysis/             rejected rows and analysis tables
manifests/                         run state, uploads and completion receipt
README.md, dataset.yaml, stats.json   dataset card, config and statistics
assets/                            the geographic density map
```

The polygon schema grows with each opt-in stage:

| Schema | Stage | Adds |
| --- | --- | --- |
| `v1.3` | `run-all` (default) | tags, geometry, provenance, extracted text, text status, word counts |
| `v1.4` | `--detect-languages` / `detect-languages` | `website_language`, `contact_website_language` and their probabilities |
| `v1.5` | sentence segmentation | `*_sentences`, `*_sentence_count`, `*_sentence_status` per field |

For the full layout and the publication contract, read [Data and remotes](docs/data-and-remotes.md).

## Load the dataset

```python
from datasets import load_dataset

dataset = load_dataset("NoeFlandre/osm-polygon-website-tag")
```

## Publish on purpose

A local run does not become public automatically. First, inspect a complete run:

```bash
uv run --locked osm-polygon-website-tag verify-results \
  --run-dir /path/to/writable/runs/website-v1
uv run --locked osm-polygon-website-tag publish-plan \
  --run-dir /path/to/writable/runs/website-v1 \
  --repo-id NoeFlandre/osm-polygon-website-tag
```

Get a separate approval. Then authenticate with `hf auth login` and add `--apply` to `publish`. The CLI reads credentials from the environment or from the local Hugging Face credential store. It does not accept a token as a command-line argument. For the publication contract, read [Data and remotes](docs/data-and-remotes.md).

The optional [Trackio Space](https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics) shows a small set of aggregate snapshot metrics. You update it explicitly from a verified snapshot. It receives no website text and no credentials.

## Development

The repository is a `src/` package with mirrored hermetic tests:

```text
src/osm_polygon_website_tag/   pipeline, contracts, storage, web, reporting
tests/                          unit, integration, and architecture checks
docs/                           MkDocs Material documentation
```

Useful commands:

| Task | Command |
| --- | --- |
| Full quality suite (fast) | `just check` |
| Completion QA gauntlet | `just qa-gauntlet` |
| Tests | `just test` |
| Unit tests | `just unit` |
| Acceptance tests | `just acceptance` |
| Architecture checks | `just architecture` |
| Lint and format | `just lint` / `just format-check` |
| Type checking | `just typecheck` |
| Hooks | `just pre-commit` / `just pre-push` |
| Documentation | `uv run --locked mkdocs build --strict` |
| Container smoke test | `just docker-smoke` |

Ruff, `ty`, pytest, pre-commit, Just, Docker, and GitHub Actions make the workflow reproducible and reviewable. The detailed [CLI reference](docs/cli.md) and the [architecture guide](docs/architecture.md) explain the implementation boundaries. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the gates that a pull request must pass. Read [SECURITY.md](SECURITY.md) to report a vulnerability.

## License and data boundaries

- **Source code:** [Apache-2.0](LICENSE).
- **OSM-derived dataset:** [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), with OpenStreetMap and Geofabrik attribution in the dataset card.
- **Website text:** fetched third-party content. The terms of its publishers apply. This project gives no additional reuse rights.
- **Language model:** the GlotLID binary is local operational state under the data root. It is not committed to Git. It is not in the dataset.
- **Source PBFs:** read-only inputs. The operator supplies them explicitly. The pipeline writes run artifacts to a separate output root. They are not committed to Git.

## Citation

Cite the project with [`CITATION.cff`](CITATION.cff). The GitHub action **Cite this repository** and the Hugging Face dataset card both show the machine-readable citation. In BibTeX:

```bibtex
@software{flandre_osm_polygon_website_tag,
  author  = {Flandre, Noé},
  title   = {{OSM Polygon Website Tag}},
  version = {0.1.0},
  license = {Apache-2.0},
  url     = {https://github.com/NoeFlandre/osm-polygon-website-tag}
}
```
