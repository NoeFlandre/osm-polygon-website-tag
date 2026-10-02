# AGENTS.md

This file gives the conventions for automated coding agents in this repository. Human contributors: [CONTRIBUTING.md](CONTRIBUTING.md) has the same gates in short form.

## Ground rules

1. **YAGNI.** Do not add abstractions, modules, dependencies, or config keys for features that nobody needs now. If you are not sure, do not add it.
2. **Modular.** Each module has one clear purpose and a small public surface. Use pure functions. Use a class only when the behaviour needs state.
3. **Typed.** All new code must pass `uv run ty check src tests`. Add type hints to each signature (parameters and return).
4. **Tested.** New behaviour comes with a pytest test. Keep the tests fast and hermetic. Make no real network calls. Do not write to the disk outside `tmp_path`.
5. **Documented.** A future agent or human must understand the code. If the code alone does not show something, write it in this file, in `docs/`, or in a docstring.
6. **Untrusted URLs.** Send each OSM website value through `web_fetch.py`. Do not bypass its checks for scheme, redirect, DNS/IP, timeout, or response size.

## Environment

- Only `uv` manages Python. Do not call `pip` directly.
- The project uses a `src/` layout. The tests import the installed package (`osm_polygon_website_tag`). They do not use relative paths from `src/`.
- Use the root Just recipes as the standard command interface. The recipes call Python tools through `uv run --locked`. This way, the locked `.venv` is always in use.
- Generated runs, model caches and bundles stay under the data root (`OSM_POLY_DATA_DIR`, default `./data`). The production PBFs are immutable read-only inputs. The operator supplies them with `--source-root`. Do not use that source tree as an output location.

## Quality gates

Before you report that the work is done, you MUST run these commands. They MUST pass.

```bash
just check
just pre-commit
just pre-push
```

The underlying gates are Ruff lint/format, ty, pytest, and `uv build`. GitHub Actions runs `just qa-pr` (baseline, Ruff, ty, the instrumented pytest suite with coverage, the CRAP gate, `uv build`, and `pip-audit`). It also runs the scoped mutation matrix, the docs build and the container smoke test. `ci-ok` is the one check that summarizes them. If you skip a check on purpose, say so in the final report.

## Style

- Line length: 100 (set in `pyproject.toml`).
- Quotes: double quotes. Indent: 4 spaces.
- Imports: `ruff` sorts them (isort profile).
- Give each module one public concern. If the name of a module does not describe its one responsibility, split the module.

## Add a dependency

1. Add the dependency to the correct section in `pyproject.toml` (runtime: `[project.dependencies]`, dev: `[dependency-groups].dev`).
2. Run `uv sync` to refresh `uv.lock`.
3. Write the dependency in `README.md` only if a human user must install extra software on the whole system.

## Add a module

1. Put the module in the subpackage that has the matching documented responsibility.
2. Add tests in the mirrored test directory.
3. Obey the dependency boundaries that `tests/architecture/test_boundaries.py` enforces.
4. When the responsibility or the entry points of the subpackage change, update the README of the subpackage.

## Secrets

- Do not commit a populated `.env`. The committed template is `.env.example`.
- Do not log or print `HF_TOKEN` or any value from `Settings`.
- To add a credential, send it through `config.Settings`. This way, the code loads it from the environment. Do not hard-code it.
