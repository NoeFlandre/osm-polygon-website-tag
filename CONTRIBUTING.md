# Contributing

Thanks for helping. This repository fetches untrusted websites and publishes a
public dataset, so its quality gates are strict. This page lists them so you
meet them locally instead of in CI.

## Setup

```bash
uv sync --locked
uv run --locked pre-commit install
```

Tests are hermetic: no real network calls, no writes outside `tmp_path`, and
no Hugging Face contact.

## The gate tiers

| Tier | Run | What it runs |
| --- | --- | --- |
| 1 | `just focused` | Only the tests the current diff can plausibly break |
| 2 | `just qa-push` | Ruff, `ty`, then the focused tests (the pre-push hook) |
| 3 | `just qa-pr` | Lock check, Ruff, `ty`, the full suite with coverage (≥ 75%), the CRAP gate (every function < 6) and `uv build` |
| 4 | `just qa-merge` | `qa-pr` plus the container smoke test |

Mutation testing runs beside `qa-pr` in CI, scoped to the functions a change
touches. Reproduce it with `just mutation-scope`. Every mutant on changed code
must be killed unless it is already listed in
[`docs/quality/mutation-baseline.txt`](docs/quality/mutation-baseline.txt);
never add new names to that file, only remove the ones your tests kill.

Also run `uv run --locked mkdocs build --strict` when you change `docs/`.

## Rules of the code

- Write the failing test first, then the change.
- Every URL taken from OSM goes through `web/web_fetch.py`. Never bypass its
  scheme, redirect, DNS/IP, timeout or size checks.
- Keep the dependency boundaries in `tests/architecture/test_boundaries.py`,
  and update a subpackage's README when its responsibility changes.
- Route credentials through `runtime.config.Settings`; never log `HF_TOKEN`.
- Style: Ruff, line length 100, full type hints on every signature.

## Commits and pull requests

One issue per commit, with a short conventional prefix (`fix:`, `perf:`,
`test:`, `docs:`, `ci:`, `build:`, `refactor:`) and the reason in the body.
Reference the issue (`Closes #NN`). The pull request template has the
checklist.

## Releasing

1. Bump `version` in `pyproject.toml`, `CITATION.cff` and the README's BibTeX
   (a test checks they agree), and move the `[Unreleased]` entries in
   [CHANGELOG.md](CHANGELOG.md) under the new version.
2. Merge that to `main`, then tag it: `git tag v1.2.3 && git push origin v1.2.3`.
3. The `release` workflow checks the tag equals `v` + the pyproject version,
   builds the sdist and wheel, and attaches them to a GitHub Release.

## Reporting a vulnerability

Do not open a public issue. See [SECURITY.md](SECURITY.md).
