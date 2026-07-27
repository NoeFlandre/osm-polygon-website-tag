# Architecture

This document describes how the codebase is organised as of the initial
scaffold. It will evolve as the extraction and analysis pipeline is built.

## Guiding principles

- **YAGNI.** We do not pre-build abstractions for hypothetical future needs.
- **Modular.** Each module has one responsibility and a small, documented
  public surface.
- **Typed and tested.** Every module ships with type hints and at least one
  pytest test.
- **Data lives outside the repo.** The git repository contains only code,
  config, and documentation. Working data lives on the external drive.

## High-level shape

```
                       ┌──────────────────────────────┐
   OSM sources ───────▶│  extraction (future)         │
   (Overpass, PBF)     │  src/osm_polygon_website_tag/│
                       │  extract.py                  │
                       └──────────────┬───────────────┘
                                      │ raw artifacts
                                      ▼
                       ┌──────────────────────────────┐
                       │  analysis (future)           │
                       │  src/osm_polygon_website_tag/│
                       │  analyze.py                  │
                       └──────────────┬───────────────┘
                                      │ exports
                                      ▼
                       ┌──────────────────────────────┐
                       │  Hugging Face dataset        │
                       │  NoeFlandre/osm-...          │
                       └──────────────────────────────┘
```

## Module responsibilities (current)

| Module                            | Responsibility                                                       |
| --------------------------------- | -------------------------------------------------------------------- |
| `osm_polygon_website_tag.config`  | Typed runtime configuration. Loads from environment + `.env`.        |
| `osm_polygon_website_tag.paths`   | Resolves the local data root and its sub-directories (`raw`, `processed`, `exports`). The single place that knows where data lives on disk. |

## Module responsibilities (planned)

| Module (future)                              | Responsibility                                                                  |
| -------------------------------------------- | ------------------------------------------------------------------------------- |
| `osm_polygon_website_tag.extract.overpass`   | Query the Overpass API for polygons with a `website` tag.                       |
| `osm_polygon_website_tag.extract.pbf`        | Parse Geofabrik/planet PBF extracts locally for large-scale queries.            |
| `osm_polygon_website_tag.analyze`            | Clean, validate, and derive statistics from extracted polygons.                 |
| `osm_polygon_website_tag.export`             | Serialize analysis outputs to parquet/CSV/GeoJSON in the `exports` directory.   |
| `osm_polygon_website_tag.publish`            | Push the `exports` directory to the HF dataset via the `hf` CLI.               |

These will be added when needed; no scaffolding exists yet.

## Dependency direction

```
config  ──▶  paths
                  ▲
                  │
            (other modules will depend on both)
```

`paths` is the leaf; it depends on nothing inside the package. `config`
depends on `paths` only to expose `resolved_data_root()`. Future modules
should depend on `config` and `paths`, never the other way around.

## Testing strategy

- Unit tests live in `tests/` and mirror the package layout.
- Tests must be hermetic: use `tmp_path` and `monkeypatch` to avoid touching
  the real external drive.
- No test should require network access. If a future test needs to verify
  against Overpass, gate it behind a `pytest.mark.network` marker and
  register it as opt-in.
