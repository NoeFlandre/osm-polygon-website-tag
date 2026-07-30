# Project Tooling Standard Integration

## Goal

Make uv, Ruff, ty, pytest, pre-commit, Typer, Rich, tqdm, Just, and
GitHub Actions active, documented parts of this project without changing the
data pipeline, persisted artifacts, or public CLI command contract.

## Existing Baseline

The project already uses uv and `uv.lock`, Ruff, ty, and pytest. It currently
uses argparse for its CLI and has no pre-commit configuration, Justfile, or
GitHub Actions workflow.

## Dependencies

Runtime dependencies will add:

- `typer`, because the installed application CLI imports it;
- `rich`, because terminal-aware presentation imports it directly;
- `tqdm`, because interactive `run-all` progress imports it directly.

The development dependency group will add `pre-commit`. Ruff, ty, pytest, and
pytest-cov remain development dependencies. All Python packages remain managed
and locked by uv; no requirements file or direct pip workflow will be added.

## Typed CLI

`application.cli` will expose an explicit `typer.Typer` object named `app`.
All eleven existing command names, positional arguments, option names,
defaults, requiredness, dry-run semantics, and result exit codes will remain.

The installed console script will continue to call `main`. The compatibility
function `main(argv: list[str] | None = None) -> int` will invoke the Typer app
without standalone exception handling, translate expected usage and
application errors into the existing integer return-code contract, and remain
directly usable by tests and library callers.

Machine-readable JSON will remain unstyled on stdout. Rich will own
human-facing stderr errors and informational output with terminal
auto-detection, so redirected output contains no ANSI control sequences.
Credentials and secrets must never be rendered.

Typer's explicit app will be tested with `typer.testing.CliRunner`, while
compatibility tests will continue exercising `main([...])`.

## Progress

The `run-all` command will use a small application-level progress adapter:

- non-interactive stderr retains the current complete line messages, preserving
  logs and automation;
- interactive stderr uses tqdm for the per-PBF `[current/total]` messages;
- phase messages that do not contain a PBF counter remain visible;
- the workflow's callback interface and emitted message strings remain
  unchanged;
- progress is always closed, including on errors and `KeyboardInterrupt`.

Rich and tqdm will not be introduced below the application layer. Library
functions remain free of terminal dependencies and continue accepting the
existing progress callback.

## Just Command Facade

A root `justfile` will define:

- `sync`: `uv sync --locked`;
- `lint`: Ruff check;
- `format`: Ruff formatting;
- `format-check`: Ruff formatting verification;
- `typecheck`: ty over `src` and `tests`;
- `test`: pytest;
- `build`: uv build;
- `check`: lock verification, lint, format-check, typecheck, test, and build;
- `pre-commit`: run every configured hook;
- `install-hooks`: install both pre-commit and pre-push hooks.

Recipes will call uv-managed commands and stop on the first failure. The
underlying uv commands remain documented for environments where Just is not
installed.

## Pre-commit

`.pre-commit-config.yaml` will use local hooks so every Python tool executes
from the uv-locked project environment:

- Ruff check with safe fixes on changed Python files;
- Ruff format on changed Python files;
- ty over `src` and `tests`, without filename forwarding;
- the complete pytest suite at the `pre-push` stage, without filename
  forwarding.

Hook installation will be explicit through `just install-hooks`; repository
setup will document that both hook types are installed. No hook may access
production PBFs, Hugging Face, or the network during normal execution.

## GitHub Actions

`.github/workflows/quality.yml` will run for pushes to `main` and pull
requests. It will:

1. check out the repository;
2. install uv through the official pinned `astral-sh/setup-uv` action;
3. install a fixed Just release using the installation approach documented by
   the Just project;
4. run `uv sync --locked`;
5. run `just check`.

Third-party actions will be pinned to immutable commit SHAs with version
comments. CI receives no dataset or Hugging Face credentials and performs no
publication.

## Documentation and Tests

README, setup documentation, AGENTS.md, and application package documentation
will explain the canonical commands and the division of responsibilities.
Tests will characterize the existing argparse command contract before
migration, then verify Typer command discovery, option compatibility, stderr
errors, JSON stdout, terminal/non-terminal progress, and interrupt cleanup.

Configuration tests will parse the Justfile, pre-commit configuration, and
workflow sufficiently to ensure required commands remain aligned. They will
not duplicate third-party parsers or test external services.

## Acceptance Criteria

- All ten requested tools are actively used and documented.
- All eleven CLI commands and existing options remain available.
- Existing `main(argv) -> int` behavior remains covered.
- JSON output remains machine-readable and progress remains on stderr.
- Non-TTY progress logs remain stable; interactive progress uses tqdm.
- Local hooks and GitHub Actions run the same uv-locked quality commands exposed
  by Just.
- CI has no write or publication permissions beyond repository checkout.
- Full pytest, ty, Ruff, pre-commit, Just, build, and isolated installed-CLI
  checks pass.
- The work is reviewed, committed, and pushed to the sole `main` branch.
