# Getting started

This guide shows how to set up a fresh clone. The [CLI reference](cli.md) gives the command options. [Operations and resume](operations.md) explains where large runs and uploads are stored.

## Prerequisites

| Tool | Version | Reason |
| --- | --- | --- |
| Python | 3.12 | `.python-version` selects it. `uv` manages the environment. |
| `uv` | 0.5 or later | Runs the locked dependencies and tools (`brew install uv`). |
| `just` | 1.50 or later | Runs the project commands (`brew install just`). |
| Git | Any current version | Clones the source and installs the hooks. |
| `hf` | Optional | Logs in to Hugging Face for an approved upload (`brew install hf`). |
| `ssh` and `rsync` | Grid'5000 only | Transfer the pinned checkout and one bundle to and from a site frontend. |
| Docker | Optional | Builds the reproducible image and runs the smoke test. |

The Python dependencies, including Trafilatura, come from `uv.lock`. Do not install them globally with `pip`. You need a Hugging Face account and a write token only for publication.

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

If `uv` cannot find Python 3.12, run `uv python install 3.12` one time. Then run `just sync` again.

## First local run

Use a read-only source mount and a separate writable output root. Publication is off, unless you supply `--apply`:

```bash
uv run --locked osm-polygon-website-tag run-all \
  --source-root '/path/to/read-only/pbf-root' \
  --output-root '/path/to/writable/runs' \
  --run-id 'website-v1'
```

After an interruption, repeat the command with the same roots and the same run ID. The pipeline records source fingerprints. It resumes the verified extraction, enrichment, and upload checkpoints. For the exact safety rules, read [Operations and resume](operations.md).

## Docker workflow

The multi-stage image uses the Python 3.12 image that a digest pins, and the `uv.lock` setup. It runs as an unprivileged `app` user. Its default command is the harmless CLI help. The smoke test reads no PBF and uses no credentials:

```bash
just docker-build
just docker-smoke
```

### Data layout

The runtime image sets `OSM_POLY_DATA_DIR=/data`. It declares four mount points. Only the raw input is read-only:

| Path | Purpose | Mount |
| --- | --- | --- |
| `/data/raw` | PBF sources | read-only |
| `/data/runs` | run output and checkpoints | writable |
| `/data/models` | GlotLID and SaT model caches (`/data/models/glotlid`, `/data/models/sat`) | writable |
| `/data/grid5000` | Grid'5000 bundles | writable |

The image does not contain production PBFs, generated runs, `.env` files, or tokens. The container user must be able to write to the runs directory and to the models directory. For `docker run`, pass `--user "$(id -u):$(id -g)"`. Compose reads `HOST_UID` and `HOST_GID`. This way, the files on the host belong to you.

### With Compose

`compose.yaml` runs the same read-only, non-root container with these mounts. Use `OSM_RAW_DIR`, `OSM_RUNS_DIR`, `OSM_MODELS_DIR` and `OSM_GRID5000_DIR` to point it at your directories. The defaults are `./data/raw`, `./data/runs`, `./data/models` and `./data/grid5000`:

```bash
mkdir -p data/raw data/runs data/models data/grid5000
HOST_UID="$(id -u)" HOST_GID="$(id -g)" OSM_RAW_DIR=/path/to/pbf-root \
  docker compose run --rm pipeline run-all \
  --source-root /data/raw --output-root /data/runs \
  --run-id geofabrik-website-v1 \
  --repo-id NoeFlandre/osm-polygon-website-tag
```

Language detection works in the same way after you mount `/data/models`. Add `--detect-languages` to `run-all`. To use the standalone stage, run `docker compose run --rm pipeline detect-languages --help`.

### Secrets

Publication needs `HF_TOKEN`. A dry run does not need it. Do not put the token in an image. Do not commit it. Export it and pass it through. Or keep it in a git-ignored `.env` file. Compose reads that file when it is present (`.env.example` shows the names):

```bash
export HF_TOKEN=...            # or put HF_TOKEN=... in .env
docker compose run --rm pipeline run-all --apply \
  --source-root /data/raw --output-root /data/runs \
  --run-id geofabrik-website-v1 --repo-id NoeFlandre/osm-polygon-website-tag
```

With plain `docker run`, pass the token by name (`--env HF_TOKEN`). This way, the token does not appear in the command line:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --user "$(id -u):$(id -g)" \
  --env HF_TOKEN \
  --mount type=bind,src=/path/to/pbf-root,dst=/data/raw,readonly \
  --mount type=bind,src="${OSM_POLY_DATA_DIR:-./data}/runs",dst=/data/runs \
  --mount type=bind,src="${OSM_POLY_DATA_DIR:-./data}/models",dst=/data/models \
  osm-polygon-website-tag:local run-all \
  --source-root /data/raw --output-root /data/runs \
  --run-id geofabrik-website-v1 \
  --repo-id NoeFlandre/osm-polygon-website-tag --apply
```

To refresh the base images that a digest pins is a dependency-maintenance change. Before you change `Dockerfile`, review the new multi-platform digests. Run the full quality checks and the Docker smoke checks again.

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
| Tier 1 focused tests | `just focused [base]` |
| Tier 2 pre-push gate | `just qa-push [base]` |
| Tier 3 pull-request gate | `just qa-pr` |
| Tier 4 merge gate | `just qa-merge` |
| Tier 4 release gate | `just release-verify <run-dir>` |
| Build distributions | `just build` |
| Coverage gate | `just coverage` |
| CRAP complexity gate | `just crap` |
| Full mutation sweep | `just mutation` |
| Changed-module mutation gate | `just mutation-scope <base>` |
| Completion QA gate | `just qa-gauntlet` |
| CI quality gates | `just qa-pr` plus one `just mutation-module <filter>` shard per changed module |
| Strict docs build | `uv run --locked mkdocs build --strict --site-dir /tmp/osm-polygon-website-tag-site` |

All Python tools run in the locked `uv` environment. If a hook fails, run the named recipe directly. Fix the reported problem and try again. Do not bypass the hooks with `--no-verify`.

The completion QA gate `just qa-gauntlet` runs these checks in this order: baseline lock checks, Ruff, `ty`, unit tests, acceptance tests, architecture tests, CRAP scoring (maximum 6), mutations, smoke test, and diff review. The command uses `just quality` for the CRAP and mutation reports. It uses `just check` for the fast core quality baseline. This way, all project-wide safeguards stay in place.

## Tiered quality gates

Each tier catches one class of mistake at the cheapest moment. Each tier adds a proof that the tier below it did not give. No tier hides a failure. A later mandatory tier runs what a cheap tier skips.

| Tier | When | What runs | Budget |
| --- | --- | --- | --- |
| 1 | `git commit` | Ruff lint and format on the commit, `ty`, and `just focused` (only the tests that the diff can break) | seconds |
| 2 | `git push` | `just qa-push`: Ruff, `ty`, and the same bounded selection | under a minute |
| 3 | pull request | `just qa-pr`: lock baseline, Ruff, `ty`, one instrumented run of the whole suite (package and quality scripts), CRAP strictly below 6. In parallel: the container smoke test and one mutation shard for each changed function | minutes |
| 4 | merge / release | `just qa-merge` locally, and `just release-verify <run-dir>` before publication | minutes |
| 5 | nightly 03:00 UTC or manual | the exhaustive mutation sweep, in shards for each package area | hours |

`scripts/quality/select_tests.py` makes the test selection for tiers 1 and 2. A changed module selects its mirrored test *package*. It does not guess a file name. The reason is that test files do not always mirror a module one to one. For example, `test_basemap_private.py` covers `geographic/basemap.py`. A change to `pyproject.toml`, `uv.lock`, `justfile`, `.pre-commit-config.yaml` or `tests/conftest.py` defeats the selection. The selector reports `BROAD`. The hook then falls back to the structural tests. Tier 3 runs the full suite.

**Tier 2 does not run the full suite.** This is on purpose. It is not a gap. The pull-request gate runs the full suite on each commit. If tier 2 runs it again on each push, you wait longer and you get no more safety.

**Tier 4 is never sampled or selected away.** `just release-verify` computes the card again. It verifies each shard. It proves the publication plan and does not upload. Publication still needs an explicit `--confirm-repo`. The remote data-identity checks refuse a release when the local data does not match the data on the Hub.

When these tiers started, we removed two duplications. First, the pull-request gate ran the whole suite one time plainly and one time with coverage. Second, both the quality job and a separate Docker job built the container image. For this reason, `crap` does not depend on `coverage` now. `qa-pr` puts them in sequence, so the suite is instrumented exactly one time. The Quality workflow also cancels superseded pull-request runs. Before, such a run left a matrix of twenty-five mutation jobs for a commit that nobody waited for. In the branch protection, require only the `ci-ok` check. It waits for each other Quality job. It accepts a skipped job.

## Mutation testing

A full sweep is about fourteen thousand mutants. It takes several hours. A hosted CI runner does not always survive this. CI first runs `just qa-pr` for the gates that are not mutation gates. Then it resolves the changed and relevant filters in sorted order. It runs one `just mutation-module <filters>` job for each shard.

A module is the unit of *selection*, but it is a poor unit of *work*. `reporting/card_stats.py` alone has 819 mutants. It ran for 88 minutes while each other shard was complete long before. So the matrix was as slow as its worst module. For this reason, `mutation_scope.py` expands a whole-module scope into its individual functions. It groups them in shards of at most `SHARD_FUNCTIONS`. Each mutmut mutant belongs to a function. The tool does not mutate module-level code. So the filters for each function cover exactly what the whole-module filter covered. If the tool cannot list the functions of a module, the module keeps its `.*` filter. The tool does not narrow it silently. A changed test file mirrors its source module. So, if you weaken a test, the tool checks that module again. Run the full `just mutation` sweep locally before a release or after a broad refactoring.

Both paths end in `just mutation-gate`. It fails on any unverified mutant that [`docs/quality/mutation-baseline.txt`](https://github.com/NoeFlandre/osm-polygon-website-tag/blob/main/docs/quality/mutation-baseline.txt) does not record. That baseline is a backlog. It is not a licence. The gate searched the results with ripgrep, and the CI image does not have ripgrep. So the gate never failed, and the backlog grew without notice. To shrink the backlog, pick a module. Write the tests that kill its mutants. Delete the lines that they cover. To regenerate the baseline from a full sweep, use `scripts/quality/mutation_baseline.py`.

## Public documentation

MkDocs Material builds `docs/` on each push to `main`. GitHub Actions deploys the strict build to [GitHub Pages](https://noeflandre.github.io/osm-polygon-website-tag/). Before the first deployment, set the Pages source of the repository to **GitHub Actions** (`Settings → Pages → Build and deployment → Source`).

## Storage defaults

The source root is any read-only directory of `.osm.pbf` files. Pass it with `--source-root`. The generated-data root (runs, model caches, Grid'5000 bundles) comes from `OSM_POLY_DATA_DIR`, in the environment or in `.env`. The default is `./data`. The explicit `--output-root` of the CLI still controls the run location. It must stay outside the source root.

On the maintainer's machine, `just` also keeps its UV package cache on the project volume when that volume is mounted. An explicitly set `UV_CACHE_DIR` has priority. Direct `uv` commands can use the same cache when you export that variable first.

For Hugging Face publication, authenticate with `hf auth login`. Then follow [Data and remotes](data-and-remotes.md). The CLI reads credentials from the environment or from the local Hugging Face store. It does not read a token from an option.

## Grid'5000 language jobs

Use `scripts/grid5000/` only after the local locked environment and the pinned model are ready. The workflow keeps the run, the model cache, the bundle, and the receipts under the data root. It transfers one shard and its checkpoint prefix to Grid'5000. Then it runs `grid5000-run` on one reserved GPU node that has no network access. The OAR wrapper requests one GPU for 30 minutes. It enforces a detection budget of 25 minutes. The staged bundle is small on purpose, and you can resume it. Several different GPU jobs can process the next bundles.

Read [Operations and resume](operations.md#run-language-detection-on-grid5000) for the sequence of the policy check, transfer, monitoring, synchronization, and cleanup.

**WARNING:** Do not start the detector or a bulk data process on a Grid'5000 frontend.

## Troubleshooting

- `pytest` cannot import the package: run `just sync` again. The project has a `src/` layout. The package is imported from the installed environment.
- `ty` reports a typing problem in a third-party package: keep the diagnostic narrow. Do not disable the unresolved-import check for the whole repository.
- `just` is missing: install it with `brew install just`.
