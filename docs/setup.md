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

All Python tools run inside the locked `uv` environment. If a hook fails, run
the named recipe directly, fix the reported issue, and retry; do not bypass
hooks with `--no-verify`.

The completion QA gate `just qa-gauntlet` runs baseline lock checks, Ruff,
`ty`, unit tests, acceptance tests, architecture tests, CRAP scoring (max 6),
mutations, smoke test, and diff review, in that order. The command uses
`just quality` for CRAP/mutation reporting and `just check` for the fast
core quality baseline, so all project-wide safeguards stay in place.

## Tiered quality gates

Each tier exists to catch a class of mistake at the cheapest moment that can
catch it, and to add something the tier below it did not already prove. No
tier is allowed to hide a failure: what a cheap tier skips, a later mandatory
tier runs.

| Tier | When | What runs | Budget |
| --- | --- | --- | --- |
| 1 | `git commit` | Ruff lint and format on the commit, `ty`, and `just focused` — only the tests the diff can plausibly break | seconds |
| 2 | `git push` | `just qa-push`: Ruff, `ty`, and the same bounded selection | under a minute |
| 3 | pull request | `just qa-pr`: lock baseline, Ruff, `ty`, one instrumented run of the whole suite, CRAP (max 6); beside it the container smoke test and one mutation shard per changed function | minutes |
| 4 | merge / release | `just qa-merge` locally, and `just release-verify <run-dir>` before publishing | minutes |
| 5 | nightly 03:00 UTC or manual | the exhaustive mutation sweep, sharded per package area | hours |

Test selection for tiers 1 and 2 comes from
`scripts/quality/select_tests.py`. A changed module selects its mirrored test
*package* rather than a guessed file name, because test files do not always
mirror a module one-to-one — `geographic/basemap.py` is covered by
`test_basemap_private.py`. Changing `pyproject.toml`, `uv.lock`, `justfile`,
`.pre-commit-config.yaml` or `tests/conftest.py` defeats selection entirely;
the selector reports `BROAD` and the hook falls back to the structural tests,
leaving the full suite to tier 3.

**Tier 2 never runs the full suite.** That is deliberate, not a gap: the
pull-request gate runs it on every commit, so paying for it again on every
push buys waiting rather than safety.

**Tier 4 is never sampled or selected away.** `just release-verify` recomputes
the card, verifies every shard, and proves the publication plan without
uploading. Publishing still demands an explicit `--confirm-repo`, and the
remote data-identity checks refuse a release whose local data does not match
what is on the Hub.

Two duplications were removed when these tiers were introduced: the
pull-request gate used to run the whole suite once plainly and again under
coverage, and the container image was built by both the quality job and the
Docker workflow. `crap` therefore no longer depends on `coverage` — `qa-pr`
sequences them so the suite is instrumented exactly once. The Quality and
Docker workflows also cancel superseded pull-request runs, which previously
left a twenty-five-job mutation matrix running for a commit nobody was
waiting on.

## Mutation testing

A full sweep is about fourteen thousand mutants and several hours, which a
hosted CI runner does not reliably survive. CI first runs `just qa-pr` for the
non-mutation gates, then resolves the changed/relevant filters in sorted order
and runs one `just mutation-module <filters>` job per shard.

A module is the unit of *selection* but a poor unit of *work*:
`reporting/card_stats.py` alone carries 819 mutants and ran for 88 minutes
while every other shard had long finished, so the matrix was as slow as its
worst module. `mutation_scope.py` therefore expands a whole-module scope into
its individual functions and groups them into shards of at most
`SHARD_FUNCTIONS`. Every mutmut mutant belongs to a function -- module level
code is not mutated -- so the per-function filters cover exactly what the
whole-module filter covered, and a module whose functions cannot be
enumerated keeps its `.*` filter rather than being silently narrowed. A changed
test file mirrors its source module, so weakening a test still rechecks that
module. Run the full `just mutation` sweep locally before a release or after
broad refactoring.

Both paths end in `just mutation-gate`, which fails on any unverified mutant
that [`docs/quality/mutation-baseline.txt`](https://github.com/NoeFlandre/osm-polygon-website-tag/blob/main/docs/quality/mutation-baseline.txt)
does not already record. That baseline is a backlog, not a licence: the gate
had been searching results with ripgrep, which the CI image lacks, so it never
failed and the backlog accumulated unnoticed. Shrink it by picking a module,
writing the tests that kill its mutants, and deleting the lines they cover;
regenerate it from a full sweep with
`scripts/quality/mutation_baseline.py`.

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
then runs `grid5000-run` on one reserved GPU node with no network access. The
OAR wrapper requests one GPU for 30 minutes and enforces a 25-minute detection
budget. The staged bundle is intentionally tiny and
resumable; several distinct GPU jobs can process successive bundles.

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
