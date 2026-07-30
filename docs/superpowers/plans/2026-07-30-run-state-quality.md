# Run-State Quality Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify run-state manifest ownership and remove test typing debt without changing any observable behavior.

**Architecture:** Keep `runtime/run_state.py` as the sole owner of run-state persistence. Add only narrow private serialization helpers, keep every public signature intact, and use exact artifact characterization tests to protect the persisted contract.

**Tech Stack:** Python 3.12, pytest, Astral ty, Ruff, uv, JSON, pathlib.

---

## File Map

- Modify `src/osm_polygon_website_tag/runtime/run_state.py`: consolidate deterministic manifest serialization behind private helpers.
- Modify `tests/runtime/test_run_state.py`: pin exact artifacts, normalize imports, and remove stale suppressions.
- No other production module should change.

### Task 1: Strengthen the persisted-manifest contract

**Files:**
- Test: `tests/runtime/test_run_state.py`

- [ ] **Step 1: Add an exact processed-source ordering test**

Add a test that initializes a run, records `b-latest.osm.pbf` before
`a-latest.osm.pbf`, reads `manifests/sources.json`, and asserts exact equality:

```python
assert json.loads(sources_path.read_text()) == [
    {
        "filename": "a-latest.osm.pbf",
        "mtime_ns": 1,
        "observation_row_count": 4,
        "public_row_count": 3,
        "rejection_count": 5,
        "size_bytes": 2,
        "status": "extracted",
    },
    {
        "filename": "b-latest.osm.pbf",
        "mtime_ns": 7,
        "observation_row_count": 10,
        "public_row_count": 9,
        "rejection_count": 11,
        "size_bytes": 8,
        "status": "extracted",
    },
]
```

- [ ] **Step 2: Prove the characterization test detects an ordering regression**

Temporarily reverse the sort in `record_processed_source`, run:

```bash
uv run pytest -q tests/runtime/test_run_state.py::test_processed_sources_manifest_is_deterministic
```

Expected: FAIL because the `b` entry precedes `a`. Restore production code
immediately; this mutation must never be staged.

- [ ] **Step 3: Run the focused test on the restored implementation**

Run the same command. Expected: PASS.

- [ ] **Step 4: Add exact JSON formatting assertions**

For both `expected_sources.json` and `sources.json`, assert that the text ends in
exactly one newline and that parsing and re-rendering with
`json.dumps(payload, indent=2, sort_keys=True) + "\n"` reproduces the bytes.

- [ ] **Step 5: Run the runtime tests**

```bash
uv run pytest -q tests/runtime/test_run_state.py
```

Expected: all tests pass.

### Task 2: Remove test typing and import debt

**Files:**
- Modify: `tests/runtime/test_run_state.py`

- [ ] **Step 1: Normalize imports**

Move all status constants and `source_is_unchanged` into the existing
module-level import. Delete repeated function-local imports.

- [ ] **Step 2: Replace dynamic import helper**

Delete `source_inventory_matches_via_size_mtime`. In its sole caller, use:

```python
assert source_is_unchanged(load_run(run_dir), new_fp) is False
```

- [ ] **Step 3: Remove stale suppressions**

Delete every `# type: ignore[...]` and associated obsolete `# noqa: RUF059`
from this test module. Do not replace them with other suppressions.

- [ ] **Step 4: Verify typing and focused behavior**

```bash
uv run ty check tests/runtime/test_run_state.py
uv run pytest -q tests/runtime/test_run_state.py
```

Expected: both commands pass without suppressions.

### Task 3: Consolidate manifest serialization

**Files:**
- Modify: `src/osm_polygon_website_tag/runtime/run_state.py`
- Test: `tests/runtime/test_run_state.py`

- [ ] **Step 1: Add narrow private helpers**

Add:

```python
def _source_fingerprint_payload(fp: SourceFingerprint) -> dict[str, int | str]:
    return {
        "filename": fp.filename,
        "size_bytes": fp.size_bytes,
        "mtime_ns": fp.mtime_ns,
    }


def _write_sources_manifest(state: RunState) -> None:
    entries = sorted(state.sources.values(), key=lambda entry: str(entry["filename"]))
    _atomic_write_json(state.run_dir / "manifests" / "sources.json", entries)
```

Keep both helpers private and absent from `__all__`.

- [ ] **Step 2: Reuse helpers minimally**

Use `_source_fingerprint_payload` when writing expected sources and when
starting a processed-source entry. Use `_write_sources_manifest` in
`record_processed_source` and `update_public_shard_metadata`. Do not alter
field inclusion, defaults, ordering, or write timing.

- [ ] **Step 3: Run focused tests after refactor**

```bash
uv run pytest -q tests/runtime/test_run_state.py
uv run ty check src/osm_polygon_website_tag/runtime/run_state.py tests/runtime/test_run_state.py
```

Expected: both pass.

### Task 4: Verify the whole repository

**Files:**
- Modify documentation only if current documentation became inaccurate.

- [ ] **Step 1: Check scope and formatting**

```bash
git diff --check
git diff --stat
uv run ruff check .
uv run ruff format --check .
```

Expected: no whitespace, lint, or formatting errors; only planned files changed.

- [ ] **Step 2: Run the complete type and test gates**

```bash
uv run ty check src tests
uv run pytest -q
```

Expected: all type checks and all tests pass.

- [ ] **Step 3: Verify packaging and CLI installation**

```bash
uv build
```

Install the wheel into a fresh temporary uv environment, import
`osm_polygon_website_tag.runtime.run_state`, and run
`osm-polygon-website-tag --help`. Expected: import and CLI both exit zero.

- [ ] **Step 4: Review and publish the scoped change**

Review the complete diff for behavior changes and secrets. If clean, commit
with:

```bash
git commit -m "Refine run-state manifest internals"
git push origin main
```

Finally confirm `HEAD == origin/main`, the worktree is clean, and `main` is the
only local branch.
