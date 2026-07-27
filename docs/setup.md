# Setup

Step-by-step guide to get the project running from a fresh clone.

## Prerequisites

| Tool       | Version  | Notes                                                  |
| ---------- | -------- | ------------------------------------------------------ |
| Python     | 3.12     | Managed by `uv` from `.python-version`; do not install manually. |
| `uv`       | >= 0.5   | `brew install uv`                                      |
| `git`      | any      | For cloning and pushing.                               |
| `hf` (HF CLI) | latest | `brew install hf` (optional, only for dataset uploads) |
| Hugging Face account | - | Required for dataset pushes; create a token at https://huggingface.co/settings/tokens |

## First-time setup

```bash
# 1. Clone
git clone https://github.com/NoeFlandre/osm-polygon-website-tag.git
cd osm-polygon-website-tag

# 2. Install dependencies (creates .venv automatically)
uv sync --group dev

# 3. Create your local .env from the template
cp .env.example .env
# Then edit .env and fill in HF_TOKEN if you plan to push to the dataset.

# 4. Sanity-check the install
uv run pytest
uv run ruff check .
uv run mypy src
```

## Day-to-day commands

| Action                          | Command                              |
| ------------------------------- | ------------------------------------ |
| Run tests                       | `uv run pytest`                      |
| Run tests with coverage         | `uv run pytest --cov`                |
| Lint                            | `uv run ruff check .`                |
| Auto-format                     | `uv run ruff format .`               |
| Type-check                      | `uv run mypy src`                    |
| Add a runtime dependency        | edit `pyproject.toml`, then `uv sync` |
| Add a dev dependency            | edit `pyproject.toml`, then `uv sync` |
| Update all deps                 | `uv sync --upgrade`                  |
| Open a REPL with the package    | `uv run python`                      |

## Working with the external data drive

The default data root is `/Volumes/Seagate M3/projects/osm-polygon-website-tag`.
Three things to know:

1. The directory is **not** inside the git repo. It is created on first access
   by `osm_polygon_website_tag.paths.data_root()`.
2. If the external drive is not mounted, `paths.data_root()` transparently
   falls back to `./data` (relative to the current working directory). This is
   useful for CI or a quick smoke test, but real OSM extracts will not fit.
3. Override the location by exporting `OSM_POLY_DATA_DIR=/some/other/path`.

## Pushing data to Hugging Face

See [`docs/data-and-remotes.md`](data-and-remotes.md) for the full flow.

## Troubleshooting

- **`uv sync` fails to find Python 3.12** — install it once via
  `uv python install 3.12`. `uv` will manage it from there.
- **`mypy` complains about a third-party package** — it is almost certainly
  untyped. Add it to `[[tool.mypy.overrides]]` in `pyproject.toml` with
  `ignore_missing_imports = true`, but only after confirming the package
  has no type stubs.
- **`pytest` cannot import `osm_polygon_website_tag`** — run `uv sync` again.
  The src/ layout means the package only becomes importable after install.
