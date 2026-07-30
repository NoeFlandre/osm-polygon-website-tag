# Workflow Inventory Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract read-only source and shard inventory inspection from workflow orchestration without changing observable behavior.

**Architecture:** Add `application.inventory` as the focused owner of source discovery, persisted-inventory comparison, and per-source bundle verification. Keep `application.workflow` as the orchestration entrypoint and re-export `discover_sources` through its existing import.

**Tech Stack:** Python 3.12, pathlib, collections.Counter, PyArrow Parquet, pytest, Astral ty, Ruff, uv.

---

## File Map

- Create `src/osm_polygon_website_tag/application/inventory.py`: read-only source and artifact inspection.
- Modify `src/osm_polygon_website_tag/application/workflow.py`: consume inventory functions and remove their implementations.
- Modify `src/osm_polygon_website_tag/application/README.md`: document the new boundary.
- Create `tests/application/test_inventory.py`: focused inventory contracts.
- Modify `tests/application/test_workflow.py`: retain orchestration tests and compatibility assertion.

### Task 1: Characterize source discovery

- [ ] Move the current discovery test inputs into
  `tests/application/test_inventory.py`.
- [ ] Add separate tests for deterministic recursive ordering, missing root,
  empty root, and duplicate basename reporting.
- [ ] Assert the exact duplicate error string and sorted duplicate list.
- [ ] Temporarily mutate discovery ordering in the existing implementation and
  run the ordering test to demonstrate RED; immediately restore the mutation.
- [ ] Run `uv run pytest -q tests/application/test_inventory.py` and confirm
  the restored implementation passes.

### Task 2: Characterize bundle verification

- [ ] Add a helper that writes empty Parquet shards using the three production
  schemas and returns a matching manifest/fingerprint.
- [ ] Add a happy-path test.
- [ ] Parameterize fail-closed tests for a missing manifest, fingerprint
  mismatch, missing shard, schema mismatch, row-count mismatch, and hash
  mismatch.
- [ ] Run the focused test file and confirm all characterization tests pass.

### Task 3: Introduce the inventory module

- [ ] Create `application/inventory.py` with:
  `discover_sources`, `source_inventory_matches_expected`, and
  `source_bundle_is_complete`.
- [ ] Implement duplicate discovery with `Counter`, keeping exact ordering and
  errors.
- [ ] Move the existing schema/count/hash verification without semantic edits.
- [ ] Export only those three functions through `inventory.__all__`.
- [ ] Change `workflow.py` to import these functions; use
  `source_inventory_matches_expected` when reopening a run.
- [ ] Remove the old discovery and bundle-verification implementations and
  now-unused imports from `workflow.py`.
- [ ] Assert in `test_workflow.py` that its `discover_sources` symbol is the
  same object as `inventory.discover_sources`.
- [ ] Run focused inventory and workflow tests plus focused `ty`.

### Task 4: Document and verify

- [ ] Update `application/README.md` with `inventory.py` responsibility,
  dependencies, entrypoints, and exclusion of all writes.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run ty check src tests`, and `uv run pytest -q`.
- [ ] Run `uv build`, install the wheel into a fresh temporary uv environment,
  import both application modules, and smoke-test CLI `--help`.
- [ ] Review the complete diff and staged secret scan.
- [ ] Commit as `Refine workflow inventory boundary`, push `main`, and verify
  `HEAD == origin/main`, a clean worktree, and only the local `main` branch.
