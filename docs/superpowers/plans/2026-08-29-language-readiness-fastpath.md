# Language Readiness Fast Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove redundant Parquet scans and per-row dictionary allocation from opt-in language-readiness checks while preserving all detection and resume contracts.

**Architecture:** `detect_languages.py` will inspect status and language columns once per readiness decision using bounded Arrow batches. `detect_language_shard` will reuse that inspection on its already-open source file, and source processing will delegate no-op decisions to the detector instead of inspecting each shard twice.

**Tech Stack:** Python 3.12, PyArrow Parquet, pytest, uv, Ruff, ty, mutmut, Just.

---

### Task 1: Add RED tests for the one-pass inspection seam

**Files:**
- Modify: `tests/pipeline/test_detect_languages.py`
- Modify: `tests/application/test_source_processing.py`

- [ ] **Step 1: Add the failing bounded-inspection test.**

Add a test that supplies a fake `ParquetFile` with `POLYGON_PUBLIC_SCHEMA_V1_4` and one Arrow record batch containing the six readiness columns. Record the `iter_batches` call and assert that the new internal inspection seam requests exactly those columns with `batch_size=8_192`, returns `False` for complete language pairs, and returns `True` when a successful text has no language pair.

```python
def test_language_readiness_inspection_is_one_bounded_arrow_scan() -> None:
    calls: list[dict[str, object]] = []

    class CompleteShard:
        schema_arrow = POLYGON_PUBLIC_SCHEMA_V1_4

        def iter_batches(self, **kwargs: object):
            calls.append(kwargs)
            yield pa.RecordBatch.from_arrays(
                [
                    pa.array(["success"]), pa.array(["absent"]),
                    pa.array(["eng_Latn"]), pa.array([0.9]),
                    pa.array([None], type=pa.string()), pa.array([None], type=pa.float64()),
                ],
                [
                    "website_text_status", "contact_website_text_status",
                    "website_language", "website_language_probability",
                    "contact_website_language", "contact_website_language_probability",
                ],
            )

    assert detection._inspect_language_readiness(
        cast(pq.ParquetFile, CompleteShard()), Path("shard.parquet")
    ) is False
    assert calls == [{"columns": [*detection._TEXT_STATUS_COLUMNS, *detection.LANGUAGE_COLUMN_NAMES], "batch_size": 8_192}]
```

- [ ] **Step 2: Add the failing source-processing delegation test.**

Patch `source_processing.detect_language_shard` with a detector-returning test double and patch `source_processing.shard_needs_language_detection` to raise if called. Invoke `_detect_source_shard_if_needed` with `detect_languages=True` and a valid detector. Assert the detector is called and its `changed` result is returned. This test proves the application layer no longer performs a duplicate precheck.

- [ ] **Step 3: Run the RED tests and confirm the expected failure.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py::test_language_readiness_inspection_is_one_bounded_arrow_scan tests/application/test_source_processing.py -q
```

Expected: collection succeeds, the new inspection test fails because the internal inspection function does not yet exist, and the delegation test fails because the caller still invokes `shard_needs_language_detection`.

### Task 2: Implement the minimal one-pass readiness path

**Files:**
- Modify: `src/osm_polygon_website_tag/pipeline/detect_languages.py`
- Modify: `src/osm_polygon_website_tag/application/source_processing.py`
- Test: `tests/pipeline/test_detect_languages.py`
- Test: `tests/application/test_source_processing.py`

- [ ] **Step 1: Implement the internal inspection function.**

Add `_inspect_language_readiness(parquet, shard)` that checks required status columns, iterates the appropriate readiness columns in batches of `8_192`, calls the existing `_validate_status_batch`, and evaluates each projected row with the existing `_row_needs_language_detection` predicate. For v1.3, return `True` after validating status batches without requesting language columns.

- [ ] **Step 2: Reuse the inspection in detection context preparation.**

Open one `ParquetFile`, validate its schema, call `_inspect_language_readiness` once, and return `None` when it reports a complete v1.4 shard. Remove the separate `_validate_text_statuses` and `shard_needs_language_detection` calls from `_prepare_detection_context`. Keep `shard_needs_language_detection` as a public wrapper that opens its own file and delegates to the inspection function.

- [ ] **Step 3: Remove the duplicate application precheck.**

In `_detect_source_shard_if_needed`, return immediately only when language detection is disabled; otherwise validate that a detector exists, call `detect_language_shard`, update metadata from its result, and return `result.changed`. Preserve progress output only when `result.changed` is true so complete shards keep their existing resume behavior.

- [ ] **Step 4: Run the focused tests and verify GREEN.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py tests/application/test_source_processing.py -q
```

Expected: all focused tests pass, including legacy-schema validation, terminal-status rejection, invalid language-pair rejection, checkpoint resume, and source-processing delegation.

### Task 3: Benchmark, refactor, and verify behavior

**Files:**
- Modify: `tests/pipeline/test_detect_languages.py` only if an uncovered readiness edge case is found.
- Inspect: `src/osm_polygon_website_tag/pipeline/detect_languages.py`
- Inspect: `src/osm_polygon_website_tag/application/source_processing.py`

- [ ] **Step 1: Run the synthetic readiness benchmark.**

Generate a v1.4 Parquet shard with 100,000 rows under `/private/tmp`, run the readiness function repeatedly, and record elapsed time and `iter_batches` count. Do not use the Seagate production data root, model cache, website network, credentials, or production PBFs. Do not add a wall-clock assertion to pytest.

- [ ] **Step 2: Run the full behavior suite.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest -q
```

Expected: every test passes with zero failures.

- [ ] **Step 3: Run repository quality gates.**

```bash
just check
just pre-commit
just pre-push
just crap
```

Expected: every command exits zero; CRAP remains below 6. Run with an isolated writable `UV_CACHE_DIR` if the sandbox cache is unavailable.

- [ ] **Step 4: Run scoped mutation verification.**

Run the repository mutation runner only for the changed readiness/application modules using its supported module filter, then inspect the complete result file. Every mutant in the changed lines must be killed; no survivor, timeout, or no-test result is acceptable for the touched scope.

- [ ] **Step 5: Review the diff and commit only the approved paths.**

Run `git diff --check`, inspect `git diff --stat` and the full diff, then commit the design, plan, production changes, and tests with:

```bash
git add docs/superpowers/specs/2026-08-29-language-readiness-fastpath-design.md docs/superpowers/plans/2026-08-29-language-readiness-fastpath.md src/osm_polygon_website_tag/pipeline/detect_languages.py src/osm_polygon_website_tag/application/source_processing.py tests/pipeline/test_detect_languages.py tests/application/test_source_processing.py
git commit -m "perf: avoid redundant language readiness scans"
```

### Task 4: Integrate and push the verified result

**Files:**
- No user-owned files may be staged or modified.

- [ ] **Step 1: Request an independent code review of the committed diff.**

Review the base and head SHAs, focusing on output equivalence, status/error preservation, bounded memory, and no duplicate model calls. Resolve all critical or important findings before integration.

- [ ] **Step 2: Fast-forward local main.**

From the main worktree, verify its dirty paths are unchanged, fast-forward `main` to the optimization commit, and rerun at least `just check` on the merged result.

- [ ] **Step 3: Push local main to origin.**

Run `git push origin main` only after the merged verification passes. Confirm the remote reports the expected head SHA; do not force-push and do not publish data artifacts.

- [ ] **Step 4: Clean only the temporary feature worktree and branch.**

After the push and final status check, remove the isolated optimization worktree and its local branch. Preserve the main worktree's pre-existing dirty files.
