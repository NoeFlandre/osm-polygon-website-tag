# Project Tooling Standard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate uv, Ruff, ty, pytest, pre-commit, Typer, Rich, tqdm, Just, and GitHub Actions as aligned, actively verified project tooling.

**Architecture:** Preserve the library pipeline and current CLI contract while replacing argparse dispatch with a typed Typer app. Keep terminal presentation in a small application adapter, and make Just the shared command vocabulary for developers, hooks, documentation, and CI.

**Tech Stack:** Python 3.12, uv, Typer, Rich, tqdm, pytest, Ruff, ty, pre-commit, Just, GitHub Actions.

---

### Task 1: Pin the CLI and progress contracts

- [ ] Expand `tests/application/test_cli.py` to assert all eleven command names,
  required options, rejected token flags, integer return codes, JSON stdout, and
  `error: ...` stderr.
- [ ] Add `tests/application/test_progress.py` for non-TTY passthrough,
  interactive tqdm updates, phase messages, and guaranteed close on interrupt.
- [ ] Demonstrate RED with the missing Typer `app` and progress adapter.

### Task 2: Add and lock dependencies

- [ ] Add Typer, Rich, and tqdm to runtime dependencies and pre-commit to the dev
  dependency group.
- [ ] Run `uv lock` and `uv sync --locked`.
- [ ] Verify imports and ensure unrelated locked packages are not upgraded.

### Task 3: Implement presentation and Typer CLI

- [ ] Add `application/progress.py` with one terminal-aware callback object.
- [ ] Replace argparse parser construction with an explicit `typer.Typer` app
  and eleven typed command functions.
- [ ] Retain `main(argv: list[str] | None = None) -> int`, plain JSON stdout,
  return-code behavior, dry-run defaults, and token-flag rejection.
- [ ] Use Rich for human-facing stderr only.
- [ ] Run focused CLI/progress tests and focused ty/Ruff checks.

### Task 4: Add the developer command surface

- [ ] Add `justfile` recipes for sync, lint, format, format-check, typecheck,
  test, build, check, pre-commit, and install-hooks.
- [ ] Add `.pre-commit-config.yaml` with uv-locked Ruff/ty pre-commit hooks and a
  full pytest pre-push hook.
- [ ] Add focused configuration tests that parse and assert command alignment.
- [ ] Install hooks only after tests pass; do not overwrite non-pre-commit custom
  hook content.

### Task 5: Add CI and documentation

- [ ] Add `.github/workflows/quality.yml` for pushes to main and pull requests,
  with immutable action SHAs, locked uv sync, fixed Just installation, and
  `just check`.
- [ ] Set read-only repository permissions and no secrets/publication steps.
- [ ] Update README, setup docs, AGENTS.md, and application README.
- [ ] Run configuration tests and action syntax checks.

### Task 6: End-to-end verification and publication

- [ ] Run `uv lock --check`, `just check`, `pre-commit run --all-files`, and
  `pre-commit run --all-files --hook-stage pre-push`.
- [ ] Run the complete pytest, ty, and Ruff gates independently for explicit
  evidence.
- [ ] Build and install the wheel in a fresh environment; verify Typer help,
  all commands, and library imports.
- [ ] Review the complete diff and staged secret scan.
- [ ] Commit as `Standardize project tooling`, push the sole `main` branch, and
  confirm a clean synchronized checkout.
