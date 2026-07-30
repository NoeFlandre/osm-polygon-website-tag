# Interleaved Per-PBF Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete and checkpoint each PBF remotely before processing the next while reusing every valid artifact from old partial runs.

**Architecture:** Extract one private per-source transaction from `application.workflow`. Both fresh extraction and legacy enrichment paths call it, with extraction permission explicit and run-level state transitions preserved.

**Tech Stack:** Python 3.12, pytest, PyArrow, uv, Ruff, ty, pre-commit, Just.

---

### Task 1: Prove the old ordering and migration gaps

- [ ] Add a two-source event-order test requiring
  `extract A -> enrich A -> upload A -> extract B`.
- [ ] Run it against current code and confirm RED because extraction B occurs
  before enrichment/upload A.
- [ ] Add an old-style `extracting` run fixture with source A complete and
  source B absent.
- [ ] Assert resume never calls extraction for A.

### Task 2: Introduce one per-source transaction

- [ ] Add a private result dataclass or tuple containing extracted, reused, and
  uploaded booleans only.
- [ ] Add `_process_source` that verifies/extracts, verifies, conditionally
  enriches, updates metadata, rebuilds the card, conditionally uploads, and
  checkpoints.
- [ ] Require an explicit `allow_extraction` flag and fail closed otherwise.
- [ ] Keep existing helpers and artifact formats unchanged.

### Task 3: Interleave fresh runs and preserve legacy paths

- [ ] In `initialized/extracting`, call `_process_source` once per source with
  extraction allowed.
- [ ] Update invocation counters from the private result.
- [ ] After the loop, advance through extracted, enriching, and enriched.
- [ ] In old `extracted/enriching` and complete-run migration paths, call the
  same transaction with extraction forbidden.
- [ ] Keep analysis, finalization, and full publication unchanged.

### Task 4: Harden interruption and checkpoint resumption

- [ ] Test interruption after extraction but before enrichment, then prove
  resume skips re-extraction.
- [ ] Preserve the existing interrupted-upload checkpoint test and adapt only
  its expected event order.
- [ ] Test acknowledged shards are not re-uploaded and counters remain
  invocation-local.
- [ ] Run focused workflow and progress suites.

### Task 5: Document and verify end to end

- [ ] Update README, data/remotes documentation, architecture, and workflow
  docstrings to describe the per-PBF transaction and old-run migration.
- [ ] Run `just check`, both pre-commit stages, and independent pytest/Ruff/ty
  gates.
- [ ] Build and install the wheel; smoke-test the unchanged command.
- [ ] Review and stage only scoped files, run the secret scan, commit as
  `Interleave per-PBF enrichment and uploads`, and push `main`.
- [ ] Verify clean local/remote SHAs and successful GitHub Actions.
- [ ] Do not signal or restart the active production process; provide the
  identical resume command and explain that the user must stop it gracefully
  before the new code can take effect.
