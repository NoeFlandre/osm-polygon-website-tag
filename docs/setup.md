# Getting started

This guide sets up a fresh clone. The [CLI reference](cli.md) has the command
options; [Operations and resume](operations.md) explains where large runs and
uploads live.

## Prerequisites

| Tool | Version | Why it is needed |
| --- | --- | --- |
| Python | 3.12 | Selected by `.python-version`; `uv` manages the environment. |
| `uv` | 0.5 or newer | Locked dependency and tool runner (`brew install uv`). |
| `just` | 1.50 or newer | Project command runner (`brew install just`). |
| Git | Any current version | Clone the source and install hooks. |
| `hf` | Optional | Hugging Face login for an approved upload (`brew install hf`). |
| `ssh` and `rsync` | Grid'5000 only | Transfer the pinned checkout and one bundle to/from a site frontend. |
| Docker | Optional | Reproducible image build and smoke test. |

Trafilatura and the other Python dependencies come from `uv.lock`; do not
install them globally with `pip`. A Hugging Face account and write token are
needed only for publication.

## First-time setup

```bash
git clone https://github.com/NoeFlandre/osm-polygon-website-tag.git
cd osm-polygon-website-tag

# Creates the locked .venv.
just sync

# Optional local settings; never commit a populated .env.
cp .env.example .env

# Install both Git hooks and run the repository checks.
just install-hooks
just check
```

If `uv` cannot find Python 3.12, run `uv python install 3.12` once and repeat
`just sync`.

## First local run

Use a read-only source mount and a separate writable output root. Publication
is off unless `--apply` is explicitly supplied:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root '/path/to/read-only/pbf-root' \
  --output-root '/path/to/writable/runs' \
  --run-id 'website-v1'
```

Repeat the command with the same roots and run ID after an interruption. The
pipeline records source fingerprints and resumes verified extraction,
enrichment, and upload checkpoints. See [Operations and resume](operations.md)
for the exact safety rules.

## Docker workflow

The multi-stage image uses the digest-pinned Python 3.12 and `uv.lock` setup,
runs as an unprivileged `app` user, and defaults to the harmless CLI help
command. The smoke test reads no PBF and uses no credentials:

```bash
just docker-build
just docker-smoke
```

For a local run, mount the immutable input read-only and keep generated files
on a separate writable volume:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw",dst=/data/raw,readonly \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs",dst=/data/runs \
  osm-polygon-website-tag:local run-all \
  --source-root /data/raw \
  --output-root /data/runs \
  --run-id geofabrik-website-v1 \
  --repo-id NoeFlandre/osm-polygon-website-tag
```

The image does not contain production PBFs, generated runs, `.env` files, or
tokens. For an explicitly approved upload, pass a token through the
environment only and add `--apply`:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --env HF_TOKEN \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw",dst=/data/raw,readonly \
  --mount type=bind,src="/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs",dst=/data/runs \
  osm-polygon-website-tag:local run-all \
  --source-root /data/raw --output-root /data/runs \
  --run-id geofabrik-website-v1 \
  --repo-id NoeFlandre/osm-polygon-website-tag --apply
```

Refreshing the digest-pinned base images is a dependency-maintenance change.
Review the new multi-platform digests and rerun the full quality and Docker
smoke checks before changing `Dockerfile`.

## Useful project commands

| Task | Command |
| --- | --- |
| Locked environment | `just sync` |
| Full quality suite (fast) | `just check` |
| Completion QA gauntlet | `just qa-gauntlet` |
| Unit tests | `just unit` |
| Acceptance tests | `just acceptance` |
| Architecture checks | `just architecture` |
| Lint and formatting check | `just lint` and `just format-check` |
| Type check | `just typecheck` |
| Pre-commit hooks | `just pre-commit` |
| Pre-push hook | `just pre-push` |
| Build distributions | `just build` |
| Coverage gate | `just coverage` |
| CRAP complexity gate | `just crap` |
| Targeted mutation gate | `just mutation` |
| Completion QA gate | `just qa-gauntlet` |
| Strict docs build | `uv run --locked mkdocs build --strict --site-dir /tmp/osm-polygon-website-tag-site` |

All Python tools run inside the locked `uv` environment. If a hook fails, run
the named recipe directly, fix the reported issue, and retry; do not bypass
hooks with `--no-verify`.

The completion QA gate `just qa-gauntlet` runs baseline lock checks, Ruff,
`ty`, unit tests, acceptance tests, architecture tests, CRAP scoring (max 6),
mutations, smoke test, and diff review, in that order. The command uses
`just quality` for CRAP/mutation reporting and `just check` for the fast
core quality baseline, so all project-wide safeguards stay in place.

## Public documentation

MkDocs Material builds `docs/` on pushes to `main` and deploys the strict build
through GitHub Actions to
[GitHub Pages](https://noeflandre.github.io/osm-polygon-website-tag/). The
repository's Pages source must be **GitHub Actions** (`Settings → Pages →
Build and deployment → Source`) before the first deployment.

## Storage defaults

The production source root used by the reviewed workflow is
`/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`. Generated runs
default to `/Volumes/Seagate M3/projects/osm-polygon-website-tag`; set
`OSM_POLY_DATA_DIR=/some/local/output/path` to override the generated-data root.
The CLI's explicit `--output-root` still controls the run location and must
remain outside the source root.

When that Seagate project directory is mounted, `just` automatically keeps its
UV package cache there as well. An explicitly set `UV_CACHE_DIR` still takes
precedence; direct `uv` commands can use the same cache by exporting that
variable first.

For Hugging Face publication, authenticate with `hf auth login` and follow
[Data and remotes](data-and-remotes.md). The CLI reads credentials from the
environment or the local Hugging Face store, never from a token option.

## Grid'5000 language jobs

Use `scripts/grid5000/` only after the local locked environment and pinned
model are ready. The workflow keeps the run, model cache, bundle, and receipts
on Seagate. It transfers one shard and its checkpoint prefix to Grid'5000,
then runs `grid5000-run` on a reserved node with no network access. The OAR
wrapper requests one host and two CPU cores for 30 minutes and enforces a
25-minute detection budget.

Read [Operations and resume](operations.md#run-language-detection-on-grid5000)
for the policy check, transfer, monitoring, synchronization, and cleanup
sequence. Never launch the detector or a bulk data process on a Grid'5000
frontend.

## Troubleshooting

- `pytest` cannot import the package: run `just sync` again; this is a `src/`
  layout and the package is imported from the installed environment.
- `ty` reports a third-party typing issue: keep the diagnostic narrow and do
  not disable unresolved-import checking for the whole repository.
- `just` is missing: install it with `brew install just`.
