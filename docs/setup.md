# Setup

Step-by-step guide to get the project running from a fresh clone.

## Prerequisites

| Tool       | Version  | Notes                                                  |
| ---------- | -------- | ------------------------------------------------------ |
| Python     | 3.12     | Managed by `uv` from `.python-version`; do not install manually. |
| `uv`       | >= 0.5   | `brew install uv`                                      |
| `just`     | >= 1.50  | `brew install just`                                    |
| `git`      | any      | For cloning and pushing.                               |
| `hf` (HF CLI) | latest | `brew install hf` (optional, only for dataset uploads) |
| Hugging Face account | - | Required for dataset pushes; create a token at https://huggingface.co/settings/tokens |
| Docker | current Docker Desktop/Engine | Optional, for the reproducible container workflow below. |

Trafilatura and its Python dependencies are installed from `uv.lock`; do not
install them globally with `pip`.

## First-time setup

```bash
# 1. Clone
git clone https://github.com/NoeFlandre/osm-polygon-website-tag.git
cd osm-polygon-website-tag

# 2. Install dependencies (creates .venv automatically)
just sync

# 3. Create your optional local configuration
cp .env.example .env

# 4. Sanity-check the install
just install-hooks
just check
```

## Reproducible Docker workflow

The repository also provides a multi-stage Docker build. The runtime image is
based on a digest-pinned Python 3.12 image, installs the exact versions from
`uv.lock`, runs as an unprivileged `app` user, and defaults to the harmless CLI
help command. The dev target contains the locked test/tooling environment.

Build and smoke-test the runtime image without reading any PBF or using any
credentials:

```bash
just docker-build
just docker-smoke
```

The image never copies production inputs, generated runs, `.env` files, or
tokens. Mount those explicitly when running a real workflow. Keep the source
mount read-only and store generated artifacts on a separate writable volume:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw",dst=/data/raw,readonly \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-website-tag-data/runs",dst=/data/runs \
  osm-polygon-website-tag:local run-all \
  --source-root /data/raw \
  --output-root /data/runs \
  --run-id geofabrik-website-v1 \
  --repo-id NoeFlandre/osm-polygon-website-tag
```

For an explicitly approved upload, pass the token through the environment
only; never put it in a Dockerfile or image layer:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --env HF_TOKEN \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw",dst=/data/raw,readonly \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-website-tag-data/runs",dst=/data/runs \
  osm-polygon-website-tag:local run-all \
  --source-root /data/raw --output-root /data/runs \
  --run-id geofabrik-website-v1 --repo-id NoeFlandre/osm-polygon-website-tag --apply
```

The base-image and uv-image manifest digests are intentionally pinned in
`Dockerfile`. Refreshing them is a deliberate dependency-maintenance change:
resolve new multi-platform digests, run the full quality suite and Docker smoke
test, then review the diff. The Docker smoke workflow performs only the build
and safe `--help` invocation; it never accesses production data or Hugging Face.

## Day-to-day commands

| Action                          | Command                              |
| ------------------------------- | ------------------------------------ |
| Run every CI quality gate       | `just check`                         |
| Synchronize the locked environment | `just sync`                       |
| Run tests                       | `just test`                          |
| Run tests with coverage         | `uv run pytest --cov`                |
| Lint                            | `just lint`                          |
| Auto-format                     | `just format`                        |
| Verify formatting               | `just format-check`                  |
| Type-check                      | `just typecheck`                     |
| Build distributions             | `just build`                         |
| Run every pre-commit hook       | `just pre-commit`                    |
| Run the pre-push test hook      | `just pre-push`                      |
| Install commit and push hooks   | `just install-hooks`                 |
| Build the documentation site    | `uv run --locked mkdocs build --strict --site-dir /tmp/osm-polygon-website-tag-site` |
| Add a runtime dependency        | edit `pyproject.toml`, then `uv sync` |
| Add a dev dependency            | edit `pyproject.toml`, then `uv sync` |
| Update all deps                 | `uv sync --upgrade`                  |
| Open a REPL with the package    | `uv run python`                      |

## Documentation site

The public site is built from this `docs/` directory with MkDocs Material.
The `main` branch workflow builds with strict link and configuration checks
and deploys the generated site to
[GitHub Pages](https://noeflandre.github.io/osm-polygon-website-tag/).
For a new repository, set **Settings → Pages → Build and deployment → Source**
to **GitHub Actions** once before the first deployment.

## Working with the external data drive

The immutable production source root is
`/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`.
Three things to know:

1. The PBF source root is supplied explicitly and remains read-only.
2. Run artifacts live under
   `/Volumes/Seagate M3/projects/osm-polygon-website-tag-data`.
3. Override only the artifact location with
   `OSM_POLY_DATA_DIR=/some/local/output/path`.

## Pushing data to Hugging Face

See [`docs/data-and-remotes.md`](data-and-remotes.md) for the full flow.

## Troubleshooting

- **`uv sync` fails to find Python 3.12** — install it once via
  `uv python install 3.12`. `uv` will manage it from there.
- **`ty` reports a third-party typing problem** — confirm the package's type
  information first. Prefer a narrow code correction or diagnostic-specific
  suppression; never disable unresolved-import checking repository-wide.
- **`pytest` cannot import `osm_polygon_website_tag`** — run `uv sync` again.
  The src/ layout means the package only becomes importable after install.
- **`just` is missing** — install it with `brew install just`. Just delegates
  all Python work to uv and never replaces the locked environment.
- **A Git hook fails** — run the named Just recipe directly, fix the reported
  issue, and retry. Do not bypass hooks with `--no-verify`.
