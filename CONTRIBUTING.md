# Contributing

Thank you for your help. This repository fetches untrusted websites and publishes a public dataset. Its quality gates are strict. This page lists the gates. Use it to pass the gates on your computer before CI runs them.

## Setup

```bash
uv sync --locked
uv run --locked pre-commit install
```

The tests are hermetic. They make no real network calls. They write nothing outside `tmp_path`. They do not contact Hugging Face.

## The gate tiers

| Tier | Run | What it runs |
| --- | --- | --- |
| 1 | `just focused` | Only the tests that the current diff can break |
| 2 | `just qa-push` | Ruff, `ty`, then the focused tests (the pre-push hook) |
| 3 | `just qa-pr` | Lock check, Ruff, `ty`, the full suite with coverage (at least 75%), the CRAP gate (each function below 6), `uv build` and `pip-audit` |
| 4 | `just qa-merge` | `qa-pr` and the container smoke test |

CI runs mutation testing in parallel with `qa-pr`. It tests only the functions that a change touches. To reproduce it, run `just mutation-scope`. Your tests must kill each mutant on the changed code. One exception exists: a mutant that is already in [`docs/quality/mutation-baseline.txt`](docs/quality/mutation-baseline.txt). Do not add new names to that file. Remove only the names that your tests kill.

When you change `docs/`, also run `uv run --locked mkdocs build --strict`.

Property tests (`tests/**/test_*properties.py`, Hypothesis) get their budget from `HYPOTHESIS_PROFILE`:

- `dev` (the default): 50 examples.
- `ci`: 200 examples, derandomized.
- `mutation`: 25 examples.
- `nightly`: 2000 examples.

When a property test finds a counterexample, commit it as an `@example`.

## Rules for the code

- Write the failing test first. Then write the change.
- Send each URL from OSM through `web/web_fetch.py`. Do not bypass its checks for scheme, redirect, DNS/IP, timeout, or size.
- Keep the dependency boundaries in `tests/architecture/test_boundaries.py`. When the responsibility of a subpackage changes, update the README of the subpackage.
- Send credentials through `runtime.config.Settings`. Do not log `HF_TOKEN`.
- Style: use Ruff with a line length of 100. Add full type hints to each signature.

## Commits and pull requests

Make one commit for each issue. Start the message with a short conventional prefix (`fix:`, `perf:`, `test:`, `docs:`, `ci:`, `build:`, `refactor:`). Give the reason in the body. Reference the issue (`Closes #NN`). The pull request template has the checklist.

## Release

1. Increase `version` in `pyproject.toml`, `CITATION.cff` and the BibTeX in the README. A test checks that the three values are equal. Move the `[Unreleased]` entries in [CHANGELOG.md](CHANGELOG.md) under the new version.
2. Merge the change to `main`. Then tag it: `git tag v1.2.3 && git push origin v1.2.3`.
3. The `release` workflow checks that the tag is equal to `v` and the pyproject version. It builds the sdist and the wheel. It attaches them to a GitHub Release.

## Report a vulnerability

Do not open a public issue. Read [SECURITY.md](SECURITY.md).
