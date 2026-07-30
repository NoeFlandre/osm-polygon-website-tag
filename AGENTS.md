# AGENTS.md

Conventions for AI coding agents (and humans) working in this repository.
If you are an automated agent, read this file end-to-end before making changes.

## Ground rules

1. **YAGNI.** Do not add abstractions, modules, dependencies, or config keys
   for features that are not yet needed. When in doubt, leave it out.
2. **Modular.** Each module has one clear purpose and a small public surface.
   Prefer pure functions over classes unless stateful behaviour is required.
3. **Typed.** All new code must pass `uv run mypy src` under strict mode. Add
   type hints to every signature (parameters and return).
4. **Tested.** New behaviour ships with a pytest test. Keep tests fast and
   hermetic; no real network calls, no real disk writes outside `tmp_path`.
5. **Documented.** Anything a future agent or human would not immediately
   understand from reading the code must live in this file, `docs/`, or a
   docstring.
6. **Untrusted URLs.** OSM website values must pass `web_fetch.py`; never
   bypass its scheme, redirect, DNS/IP, timeout, or response-size checks.

## Environment

- Python is managed exclusively by `uv`. Never invoke `pip` directly.
- The project uses a `src/` layout; tests import the installed package
  (`osm_polygon_website_tag`), not relative paths from `src/`.
- Run all commands through `uv run <tool>` so the locked `.venv` is used.
- Code lives on the Mac. Generated runs live in the dedicated Seagate data
  directory. Production PBFs are immutable read-only inputs supplied
  explicitly by `--source-root`; never use that source tree as an output
  location.

## Quality gates

Before declaring work done, an agent MUST run and pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

If a check is intentionally skipped, call it out explicitly in the final report.

## Style

- Line length: 100 (configured in `pyproject.toml`).
- Quotes: double quotes. Indent: 4 spaces.
- Imports: sorted by `ruff` (isort profile).
- One public concern per module. If a module's name does not describe its
  single responsibility, split it.

## Adding a dependency

1. Add it to the appropriate section in `pyproject.toml`
   (runtime → `[project.dependencies]`, dev → `[dependency-groups].dev`).
2. Run `uv sync` to refresh `uv.lock`.
3. Mention it in `README.md` only if a human user needs to install something
   extra system-wide.

## Adding a new top-level module

1. Create `src/osm_polygon_website_tag/<name>.py`.
2. Add at least one test in `tests/test_<name>.py`.
3. Update `docs/architecture.md` with the new module's responsibility and
   how it depends on existing modules.

## Secrets

- Never commit a populated `.env`. The committed template is `.env.example`.
- Never log or print `HF_TOKEN` or any value from `Settings`.
- If you need to add a credential, route it through `config.Settings` so it
  is loaded from the environment, not hard-coded.
