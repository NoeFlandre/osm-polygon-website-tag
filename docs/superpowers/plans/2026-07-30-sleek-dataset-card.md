# Sleek Dataset Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce warning-free Hugging Face metadata and a concise, factual, automatically refreshed public dataset card.

**Architecture:** Keep `compute_card_stats` as the sole source of card numbers and replace only the deterministic rendering layer. Split rendering into small private helpers for status, text metrics, hostnames, schema, and provenance.

**Tech Stack:** Python 3.12, PyArrow, pytest, Ruff, ty, uv, pre-commit, Just, GitHub Actions.

---

### Task 1: Lock the metadata contract

**Files:**
- Modify: `tests/reporting/test_card.py`
- Modify: `src/osm_polygon_website_tag/reporting/card.py`

- [ ] Add a failing assertion that neither generated artifact contains `task_categories`.
- [ ] Run the focused test and observe the official-category warning source.
- [ ] Remove the optional field without changing license, config, size category, tags, progress, or derived totals.
- [ ] Run the focused test to GREEN.

### Task 2: Lock the concise factual card contract

**Files:**
- Modify: `tests/reporting/test_card.py`
- Modify: `src/osm_polygon_website_tag/reporting/card.py`

- [ ] Add RED assertions for the compact status and website-text tables, combined word total, top-ten cap, analysis-Parquet pointer, current schema, attribution, and absence of verbose eight-cell/per-source tables.
- [ ] Extract focused render helpers and replace the long sequential renderer with the approved sections.
- [ ] Keep every numeric interpolation sourced from `CardStats`; compute combined words only as the sum of its two derived word totals.
- [ ] Run all reporting tests to GREEN.

### Task 3: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`

- [ ] Document the warning-free metadata choice and concise auto-generated card contract.
- [ ] Run `uvx --from rust-just just check`.
- [ ] Run pre-commit and pre-push stages.
- [ ] Build/install the wheel in a fresh temporary venv and smoke-test card imports and CLI help.
- [ ] Audit the diff, commit, push the sole `main` branch, and confirm GitHub Actions success.
- [ ] Confirm no pipeline process is active and provide the unchanged resume command.
