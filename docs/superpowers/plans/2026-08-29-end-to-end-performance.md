# End-to-End Performance Optimization Implementation Plan

> **For agentic workers:** Use the TDD workflow: write each failing test,
> observe RED, implement the smallest change, observe GREEN, then refactor.

**Goal:** Remove the two measured hot-path costs while preserving all existing
behavior, APIs, outputs, compatibility, and correctness.

**Architecture:** The analyzer will compute canonical hostname projections once
per DuckDB connection and reuse them for exact and top tables. The SQLite text
cache will replace its per-record read-plus-write sequence with one atomic
upsert that returns the stored row. Both changes retain existing bounded
processing and transaction boundaries.

**Tech Stack:** Python 3.12, DuckDB, SQLite, PyArrow/Parquet, pytest, uv,
Ruff, ty, Just, CRAP, and mutmut.

---

### Task 1: Add RED tests for the measured seams

**Files:**

- Modify: `tests/pipeline/test_analyze.py`
- Modify: `tests/web/test_text_cache.py`

- [ ] **Step 1: Add the failing hostname-evaluation test.**

Add a small canonical-observation fixture, wrap the existing hostname
normalizer with a call counter, invoke `_write_hostname_tables`, and assert
that each website/contact value is normalized once. Keep the assertions on
the exact and top output contracts so the test also describes the reused
projection's observable behavior.

- [ ] **Step 2: Add the failing cache single-write test.**

Record one value to create the schema, then monkeypatch the cache's `_get`
lookup to fail if called and record an updated value. Assert that the returned
value has the updated fields and incremented attempt count. The current
implementation must fail this test because it performs `_get` before its
upsert.

- [ ] **Step 3: Run only the new tests and confirm RED.**

```bash
MPL_IGNORE_SYSTEM_FONTS=1 uv run --locked pytest \
  tests/pipeline/test_analyze.py::test_hostname_analysis_normalizes_each_field_once \
  tests/web/test_text_cache.py::test_record_uses_single_upsert_without_lookup -q
```

Expected: collection succeeds and both new tests fail for the intended
pre-optimization reasons.

### Task 2: Implement and benchmark hostname materialization

**Files:**

- Modify: `src/osm_polygon_website_tag/pipeline/analyze.py`
- Test: `tests/pipeline/test_analyze.py`

- [ ] **Step 1: Implement one temporary hostname projection.**

Create a temporary table with the two normalized hostname columns from
`canonical_observations` immediately after registering the existing UDF.
Change the two exact/top query pairs to read only from that table. Keep static
column allowlisting, null handling, grouping, ordering, and atomic output
writes unchanged.

- [ ] **Step 2: Run focused tests GREEN.**

```bash
MPL_IGNORE_SYSTEM_FONTS=1 uv run --locked pytest tests/pipeline/test_analyze.py -q
```

- [ ] **Step 3: Rerun the synthetic 5,000-row benchmark.**

Compare the same pre-change baseline and candidate setup under `/private/tmp`.
Record elapsed time and confirm exact/top row equality against the baseline.
Do not add a machine-dependent wall-clock assertion to pytest.

### Task 3: Implement and benchmark the SQLite upsert

**Files:**

- Modify: `src/osm_polygon_website_tag/web/text_cache.py`
- Test: `tests/web/test_text_cache.py`

- [ ] **Step 1: Implement the explicit `RETURNING` upsert.**

Use explicit column names and the existing retry helper. Insert with attempt
count one; on conflict update the result fields, increment the existing row's
attempt count, and return all fields consumed by `_cached_text_from_row`.
Preserve validation, UTC timestamp generation, invocation IDs, batched commit
accounting, and lock behavior.

- [ ] **Step 2: Run the focused cache tests GREEN.**

```bash
MPL_IGNORE_SYSTEM_FONTS=1 uv run --locked pytest tests/web/test_text_cache.py -q
```

- [ ] **Step 3: Benchmark 10,000 temporary cache records.**

Measure the pre-change two-statement shape and the new single-statement shape
using a temporary SQLite file under `/private/tmp`. Verify inserted and
updated rows, attempt counts, returned values, and committed rows before
accepting any timing result.

### Task 4: Full regression and quality verification

**Files:**

- Inspect all changed files and their tests.

- [ ] **Step 1: Run the full behavior and repository gates.**

```bash
MPL_IGNORE_SYSTEM_FONTS=1 just check
MPL_IGNORE_SYSTEM_FONTS=1 just pre-commit
MPL_IGNORE_SYSTEM_FONTS=1 just pre-push
MPL_IGNORE_SYSTEM_FONTS=1 just crap
```

If a build dependency again requires network access, rerun that exact build
gate with network permission and preserve the environmental failure in the
report.

- [ ] **Step 2: Run scoped mutation verification.**

Run the repository mutation runner for the changed analyzer and cache modules
using its supported module filter. Inspect every touched mutant; no survivor,
timeout, or no-test result is acceptable for the changed scope.

- [ ] **Step 3: Review the diff and commit exact paths.**

Run `git diff --check`, inspect the full diff, and commit only the design,
plan, source, and test files. Use clear Conventional Commit messages.

### Task 5: Integrate and push the validated result

- [ ] **Step 1: Recheck the clean feature worktree and review the commits.**

- [ ] **Step 2: Fast-forward local `main` without staging or touching its
  pre-existing dirty files.**

- [ ] **Step 3: Rerun the final gate on merged `main`, push `origin/main`, and
  verify the remote SHA.**

- [ ] **Step 4: Remove only the temporary feature worktree and merged local
  branch after the push succeeds.**
