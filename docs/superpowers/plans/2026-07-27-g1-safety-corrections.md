# G1 Safety Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct G1 so the synthetic pipeline is bounded, transactional,
factually verified, and safe for a separately approved canary and publication.

**Architecture:** Bounded extraction writes through batch sinks and an on-disk
candidate ledger; DuckDB produces analysis artifacts under explicit resource
limits; strict run state and whole-run verification bind all publishable files
to a deterministic receipt.

**Tech Stack:** Python 3.12, pyosmium, PyArrow, DuckDB, SQLite, Shapely,
pyproj, pytest, mypy, ruff, uv.

---

### Task 1: Lifecycle, CLI, and credential contracts

**Files:**
- Modify: `src/osm_polygon_website_tag/run_state.py`
- Modify: `src/osm_polygon_website_tag/cli.py`
- Modify: `src/osm_polygon_website_tag/publish.py`
- Modify: `src/osm_polygon_website_tag/hf_token.py`
- Test: `tests/test_run_state.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_publish.py`
- Test: `tests/test_hf_token.py`

- [ ] Add focused failing tests for exact source inventory, pre-work phase
  validation, usable `init`, preservation of extractor counts, and absence of
  token CLI arguments.
- [ ] Run the focused tests and retain the expected RED output.
- [ ] Implement the smallest lifecycle and CLI changes that satisfy the
  contracts.
- [ ] Run focused tests to GREEN and refactor without changing behavior.

### Task 2: Bounded transactional extraction

**Files:**
- Create: `src/osm_polygon_website_tag/batch_sink.py`
- Create: `src/osm_polygon_website_tag/candidate_ledger.py`
- Modify: `src/osm_polygon_website_tag/extraction.py`
- Modify: `src/osm_polygon_website_tag/atomic.py`
- Test: `tests/test_extraction.py`
- Test: `tests/test_atomic.py`
- Test: `tests/test_safety.py`

- [ ] Add failing tests proving bounded pending batches, on-disk candidate
  reconciliation, pre/post mutation detection, and bundle failure recovery.
- [ ] Run those tests and retain RED evidence for each defect.
- [ ] Add bounded Arrow sinks and a run-owned SQLite ledger; write all outputs
  in a transaction directory and promote them with rollback.
- [ ] Run focused tests to GREEN, including empty shards and injected failures.

### Task 3: Bounded analysis and factual card statistics

**Files:**
- Modify: `src/osm_polygon_website_tag/duckdb_engine.py`
- Modify: `src/osm_polygon_website_tag/analyze.py`
- Modify: `src/osm_polygon_website_tag/card_stats.py`
- Modify: `src/osm_polygon_website_tag/card.py`
- Modify: `src/osm_polygon_website_tag/website.py`
- Test: `tests/test_analyze.py`
- Test: `tests/test_card.py`
- Test: `tests/test_website.py`

- [ ] Add failing tests for low-memory configuration, direct Parquet outputs,
  exact eight-cell reconciliation, bare/scheme-relative hostname parsing,
  canonical counts, corrupt-artifact failure, and zero-row sources.
- [ ] Run focused tests and retain RED evidence.
- [ ] Replace unbounded fetch/materialization paths with DuckDB aggregate/COPY
  operations and bounded scalar/top-K reads.
- [ ] Run focused tests to GREEN and verify transactional analysis promotion.

### Task 4: Whole-run verification and deterministic finalization

**Files:**
- Modify: `src/osm_polygon_website_tag/verify.py`
- Modify: `src/osm_polygon_website_tag/finalize.py`
- Modify: `src/osm_polygon_website_tag/publish.py`
- Test: `tests/test_verify.py`
- Test: `tests/test_finalize.py`
- Test: `tests/test_publish.py`

- [ ] Add failing tests for every shard class, zero counts, exact schemas,
  source inventory, analysis/card/metadata mutation, missing/extra files, state,
  and receipt determinism.
- [ ] Run the focused tests and retain RED evidence.
- [ ] Implement strict bounded verification and a receipt binding every
  publishable relative path, size, and SHA-256.
- [ ] Make publish planning receipt-driven and keep dry runs network-free.
- [ ] Run focused tests to GREEN.

### Task 5: Exact synthetic acceptance

**Files:**
- Modify: `tests/test_acceptance_3source.py`

- [ ] Build three synthetic sources covering all eight cells, both polygon
  types, a real hole and centroid shift, canonical duplicates/ties, a geometry
  rejection, an open-way exclusion, an empty public shard, and hostname edge
  cases.
- [ ] Replace every loose assertion with exact expected counts and identities.
- [ ] Add independent mutations for all receipt-bound artifact classes.
- [ ] Run acceptance tests to GREEN.

### Task 6: Full verification and handoff

**Files:**
- Modify only documentation whose claims disagree with verified behavior.

- [ ] Run `uv run ruff check .`.
- [ ] Run `uv run ruff format --check .`.
- [ ] Run `uv run mypy src`.
- [ ] Run `uv run pytest`.
- [ ] Run the configured coverage gate.
- [ ] Run `uv build`.
- [ ] Inspect the final diff and synthetic artifacts independently.
- [ ] Stop for user review before production PBF access, canary execution,
  staging, committing, pushing, network access, repository creation, or
  publication.
