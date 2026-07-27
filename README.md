# osm-polygon-website-tag

A repository for analyzing OpenStreetMap (OSM) polygons that carry a `website` tag.

The project extracts polygons (ways/relations) from OSM that have a `website=*` tag,
runs analysis on them, and publishes the resulting dataset to
[Hugging Face](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag).

> **Status:** early scaffolding. The package, tooling, and documentation are in place;
> the actual OSM extraction and analysis pipeline will land in subsequent commits.

---

## Quick start

```bash
# 1. Install uv (once, if missing)
brew install uv

# 2. Sync dependencies into an isolated .venv (uv creates it automatically)
uv sync --group dev

# 3. Run the test suite
uv run pytest

# 4. Lint and type-check
uv run ruff check .
uv run mypy src
```

All commands run inside the project `.venv`; nothing is installed globally.

## Project layout

```
.
├── pyproject.toml          # Project metadata + tooling config (ruff, mypy, pytest)
├── uv.lock                 # Locked dependency versions (created by `uv sync`)
├── README.md               # You are here
├── AGENTS.md               # Conventions for AI coding agents
├── docs/                   # Long-form documentation
│   ├── setup.md            # Detailed environment setup
│   ├── architecture.md     # How the modules fit together
│   └── data-and-remotes.md # Where data lives and how it gets to HF
├── scripts/                # Operational scripts (HF upload, data prep)
├── src/osm_polygon_website_tag/
│   ├── __init__.py
│   ├── config.py           # Typed settings (env + .env)
│   ├── paths.py            # Local data path resolution
│   └── py.typed            # Marker for PEP 561 type distribution
└── tests/                  # pytest suite
```

## Where data lives

| Concern         | Location                                                      |
| --------------- | ------------------------------------------------------------- |
| Code            | This repository (local, git)                                  |
| Raw OSM extracts | `/Volumes/Seagate M3/projects/osm-polygon-website-tag/raw`    |
| Processed data  | `/Volumes/Seagate M3/projects/osm-polygon-website-tag/processed` |
| HF-ready exports | `/Volumes/Seagate M3/projects/osm-polygon-website-tag/exports` |
| Published dataset | https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag |

The default data root can be overridden via the `OSM_POLY_DATA_DIR` environment variable.
See [`docs/data-and-remotes.md`](docs/data-and-remotes.md) for the rationale.

## License

Apache-2.0. See [LICENSE](LICENSE).
