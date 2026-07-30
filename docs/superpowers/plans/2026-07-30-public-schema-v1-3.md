# Public Polygon Schema v1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove six redundant public columns while migrating and reuploading existing shards without reopening PBFs or refetching websites.

**Architecture:** Keep immutable v1.1 and v1.2 schema constants for detection, make v1.3 the sole current public schema, and add a bounded atomic public-shard migrator. The workflow migrates a verified source bundle before enrichment/upload; the existing content-hash checkpoint naturally uploads only changed shards.

**Tech Stack:** Python 3.12, PyArrow/Parquet, SQLite enrichment cache, pytest, Ruff, ty, uv, pre-commit, Just, GitHub Actions.

---

### Task 1: Freeze the v1.3 schema contract

**Files:**
- Modify: `tests/contracts/test_polygon_schema.py`
- Modify: `src/osm_polygon_website_tag/contracts/polygon_schema.py`

- [ ] Add a failing test asserting `SCHEMA_VERSION == "v1.3"` and that the exact removed set is disjoint from `POLYGON_PUBLIC_SCHEMA.names`.
- [ ] Run `uv run pytest tests/contracts/test_polygon_schema.py -q`; verify failure against v1.2.
- [ ] Preserve `POLYGON_PUBLIC_SCHEMA_V1_1`, introduce `POLYGON_PUBLIC_SCHEMA_V1_2`, and define current v1.3 by removing only the six approved fields while retaining text fields and metadata.
- [ ] Remove preferred-field invariants and documentation; keep website inclusion/flag invariants.
- [ ] Update schema fixtures and run the contract tests to GREEN.

### Task 2: Write current-schema extraction directly

**Files:**
- Modify: `tests/pipeline/test_extraction.py`
- Modify: `src/osm_polygon_website_tag/pipeline/extraction.py`

- [ ] Change extraction assertions to require the exact v1.3 public columns while confirming comparison observations still retain raw/classified Wikidata.
- [ ] Run the focused extraction tests and observe failures from obsolete public fields.
- [ ] Stop emitting the six removed keys into public rows; retain internal Wikidata parsing for comparison observations and `area_m2`/`area_bucket`.
- [ ] Remove now-unused preferred helpers from extraction imports without changing their domain API.
- [ ] Run extraction tests to GREEN.

### Task 3: Add bounded atomic v1.2 migration

**Files:**
- Create: `src/osm_polygon_website_tag/pipeline/public_schema_migration.py`
- Create: `tests/pipeline/test_public_schema_migration.py`
- Modify: `src/osm_polygon_website_tag/pipeline/README.md`

- [ ] Write failing tests for v1.2-to-v1.3 projection, row/text/order preservation, empty shards, idempotent v1.3 handling, unknown-schema rejection, and original-file preservation when staged validation fails.
- [ ] Run the new test module and verify RED.
- [ ] Implement `migrate_public_shard(path, *, batch_rows=8192)` using `ParquetFile.iter_batches`, `BatchParquetSink`, schema-version replacement, staged validation, and atomic replacement.
- [ ] Return a typed result containing `changed`, `row_count`, and `shard_sha256`.
- [ ] Document the module boundary and run the migration tests to GREEN.

### Task 4: Integrate migration with enrichment and resumable workflow

**Files:**
- Modify: `tests/pipeline/test_enrich.py`
- Modify: `tests/application/test_workflow.py`
- Modify: `src/osm_polygon_website_tag/pipeline/enrich.py`
- Modify: `src/osm_polygon_website_tag/application/inventory.py`
- Modify: `src/osm_polygon_website_tag/application/workflow.py`

- [ ] Add RED tests proving v1.1 enrichment emits v1.3 and v1.2 migration performs no PBF extraction or web enrichment.
- [ ] Add RED workflow tests proving a migrated hash triggers exactly one upload and a matching v1.3 checkpoint skips on resume.
- [ ] Accept v1.1/v1.2/current schemas at the correct boundaries; migrate v1.2 before enrichment detection and update run metadata after any rewrite.
- [ ] Extend run-level migration detection so complete/analyzed/card-built runs enter the per-source migration path when any shard is not v1.3.
- [ ] Run focused enrichment/workflow/inventory tests to GREEN.

### Task 5: Adapt verification, reporting, and analysis consumers

**Files:**
- Modify: `tests/reporting/test_verify.py`
- Modify: `tests/reporting/test_card.py`
- Modify: `tests/pipeline/test_partition_aggregate.py`
- Modify: `src/osm_polygon_website_tag/reporting/verify.py`
- Modify: `src/osm_polygon_website_tag/reporting/card.py`
- Modify: `src/osm_polygon_website_tag/pipeline/partition_aggregate.py`
- Modify: fixture-producing tests under `tests/application`, `tests/publishing`, and `tests/reporting`

- [ ] Add RED assertions that the card excludes removed columns and verification derives inclusion solely from `website`/`contact_website`.
- [ ] Ensure public aggregation no longer requires removed Wikidata fields; preserve Wikidata analysis through comparison-observation Parquets.
- [ ] Update fixtures mechanically to current schema and run all affected focused tests.
- [ ] Confirm no production query or rendered schema table references removed public columns.

### Task 6: Update public documentation and acceptance coverage

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/data-and-remotes.md`
- Modify: `tests/acceptance/test_acceptance_3source.py`

- [ ] Document v1.3, the six removals, schema-only migration, unchanged internal analysis, and smart reupload behavior.
- [ ] Extend acceptance coverage to begin with an existing v1.2 shard and prove retained rows/text are migrated and uploaded without source/web work.
- [ ] Run the acceptance test to GREEN.

### Task 7: Verify, commit, push, and establish resume safety

**Files:**
- Verify all modified files.

- [ ] Run `uvx --from rust-just just check`.
- [ ] Run `uv run pre-commit run --all-files` and the pre-push stage.
- [ ] Build/install the wheel in a fresh temporary venv and smoke-test the CLI.
- [ ] Confirm `git diff --check`, scope, no secrets, and a clean single `main` branch.
- [ ] Commit implementation, push `main`, and wait for GitHub Actions success.
- [ ] Reconfirm no website pipeline process is active.
- [ ] Provide the unchanged resumable command; explain that local v1.2 shards will be migrated and reuploaded per PBF, while v1.3 acknowledged shards skip.
