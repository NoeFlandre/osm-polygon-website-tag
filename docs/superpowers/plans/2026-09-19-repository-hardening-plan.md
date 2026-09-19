# Repository Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve issues #6--#12, make the release/reporting code deeply modular without changing published semantics, complete the quality gates, merge the release PR, and remove only proven-stale GitHub branches and repository files.

**Architecture:** Keep artifact-derived reporting as the single source of truth. Split card orchestration, card metadata, card rendering, card byte patching, and release artifact promotion into modules with one responsibility each. Keep public builder behaviour stable through explicit compatibility tests and retain the existing spill-backed, deterministic reducers.

**Tech Stack:** Python 3.12, uv, pytest, Ruff, ty, mutmut, DuckDB, PyArrow, MkDocs Material, Just, GitHub Actions, Docker, Hugging Face Hub.

---

### Task 1: Establish the measurable baseline

**Files:**
- Inspect: `src/osm_polygon_website_tag/`, `tests/`, `pyproject.toml`, `justfile`, `.github/workflows/quality.yml`
- Create only temporary evidence under `/private/tmp/website-tag-baseline/`; do not commit logs.

- [ ] **Step 1: Record the tracked baseline and user-owned artifacts**

Run:

```bash
git status --short --branch
git diff --check
git ls-files --others --exclude-standard
git rev-list --count origin/main..HEAD
```

Expected: the isolated branch is clean, and the original checkout's untracked `reports/`, `site/`, and `slides/` are not copied into it.

- [ ] **Step 2: Record source/test sizes and names**

```bash
mkdir -p /private/tmp/website-tag-baseline
wc -l src/osm_polygon_website_tag/reporting/card.py src/osm_polygon_website_tag/reporting/card_stats.py src/osm_polygon_website_tag/reporting/geometry_stats.py src/osm_polygon_website_tag/publishing/release.py tests/reporting/test_card.py tests/application/test_source_processing.py tests/application/test_workflow.py tests/pipeline/test_extraction.py
rg -n '^def test_|^    def test_' tests > /private/tmp/website-tag-baseline/test-names.txt
```

Expected: the reported large modules are above the target size and the test-name inventory is non-empty.

- [ ] **Step 3: Record the authoritative collection count**

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-website-tag-hardening uv run --locked pytest --collect-only -q
```

If dependency resolution is blocked offline, use the existing locked interpreter with `PYTHONPATH=src`, record the exact native-import blocker, and use the AST inventory until the environment is repaired. Do not change dependency versions to bypass it.

### Task 2: Finish mutation scope and quality-gate correctness (#9)

**Files:**
- Modify: `scripts/quality/mutation_scope.py`, `scripts/quality/mutation_runner.py`
- Modify: `tests/quality/test_mutation_scope.py`, `tests/quality/test_mutation_runner.py`, `tests/architecture/test_tooling.py`

- [ ] **Step 1: Add RED tests for package initializers and empty scopes**

The mapper tests must assert:

```python
assert _source_path_for_mutant_name(
    "osm_polygon_website_tag.reporting.geographic"
) == Path("src/osm_polygon_website_tag/reporting/geographic/__init__.py")
assert _source_path_for_mutant_name(
    "osm_polygon_website_tag.reporting.geographic.__init__"
) == Path("src/osm_polygon_website_tag/reporting/geographic/__init__.py")
```

Also distinguish a genuinely empty mutation scope from generated mutants with no selected test associations.

- [ ] **Step 2: Run focused tests before implementation**

```bash
PYTHONPATH=src /Volumes/Seagate\ M3/projects/osm-polygon-website-tag/repo/.venv/bin/python -m pytest tests/quality/test_mutation_scope.py tests/quality/test_mutation_runner.py -q
```

Expected: the new initializer test fails if the current mapper still emits `geographic.py`.

- [ ] **Step 3: Implement one package-aware source mapper**

Resolve a module path by checking package directories before appending `.py`:

```python
relative = _PACKAGE_SOURCE_ROOT.joinpath(*relative_module.split("."))
if relative.is_dir():
    return relative / "__init__.py"
return relative.with_suffix(".py")
```

Keep the package-root special case, deterministic deduplication, and `only_mutate` assignment before mutmut generation.

- [ ] **Step 4: Make runner outcomes explicit and verify**

Keep failure for “mutants exist but no selected test covers any mutant”; allow success only for a truly empty generated scope. Run the focused quality, architecture, Ruff, and ty commands, then commit:

```bash
git add scripts/quality tests/quality tests/architecture/test_tooling.py
git commit -m "fix: make mutation scopes package-aware"
```

### Task 3: Remove deferred first-party imports (#12)

**Files:**
- Modify: `src/osm_polygon_website_tag/reporting/repair.py`
- Modify: `src/osm_polygon_website_tag/reporting/finalize.py`
- Modify: `src/osm_polygon_website_tag/pipeline/grid5000_sentences.py`
- Modify: `tests/architecture/test_tooling.py`

- [ ] **Step 1: Add an AST regression test**

Walk `src/**/*.py`, collect `ast.Import` and `ast.ImportFrom` nodes nested below functions, and fail for first-party module names beginning with `osm_polygon_website_tag.`. Third-party lazy imports remain allowed.

- [ ] **Step 2: Run the test before the fix**

```bash
PYTHONPATH=src /Volumes/Seagate\ M3/projects/osm-polygon-website-tag/repo/.venv/bin/python -m pytest tests/architecture/test_tooling.py -q
```

Expected: exactly the three known deferred first-party imports are reported.

- [ ] **Step 3: Hoist and de-duplicate imports**

Add `finalize_run` to the existing module-scope import in `reporting/repair.py`, import `analyze_results` at module scope in `reporting/finalize.py`, and import `segment_sentence_shard` at module scope in `pipeline/grid5000_sentences.py`. Remove nested copies without changing signatures.

- [ ] **Step 4: Prove imports and commit**

```bash
PYTHONPATH=src /Volumes/Seagate\ M3/projects/osm-polygon-website-tag/repo/.venv/bin/python -c 'import osm_polygon_website_tag.reporting.repair; import osm_polygon_website_tag.reporting.finalize; import osm_polygon_website_tag.pipeline.grid5000_sentences'
PYTHONPATH=src /Volumes/Seagate\ M3/projects/osm-polygon-website-tag/repo/.venv/bin/python -m pytest tests/architecture/test_tooling.py tests/reporting/test_repair.py tests/reporting/test_finalize.py tests/pipeline/test_grid5000_sentences.py -q
git add src tests/architecture/test_tooling.py
git commit -m "refactor: make first-party imports explicit"
```

Enable Ruff `PLC0415` only if the locked Ruff version supports it; otherwise retain the AST guard as the repository-enforced rule.

### Task 4: Separate card reporting from release promotion (#6, #10)

**Files:**
- Create: `src/osm_polygon_website_tag/reporting/card_rendering.py`
- Create: `src/osm_polygon_website_tag/reporting/card_metadata.py`
- Create: `src/osm_polygon_website_tag/reporting/card_patching.py`
- Create: `src/osm_polygon_website_tag/publishing/card_artifacts.py`
- Modify: `src/osm_polygon_website_tag/reporting/card.py`, `src/osm_polygon_website_tag/publishing/release.py`
- Modify: `src/osm_polygon_website_tag/reporting/README.md`, `src/osm_polygon_website_tag/publishing/README.md`
- Modify: `tests/reporting/test_card.py`, `tests/publishing/test_release.py`, `tests/architecture/test_boundaries.py`

- [ ] **Step 1: Add seam and YAML contract tests**

Assert that the public builder still returns `run_dir / "README.md"`, that README front matter and `dataset.yaml` parse identically with `yaml.safe_load`, and that reporting modules do not import `publishing.card_artifacts`.

- [ ] **Step 2: Move pure rendering**

Move `_render_markdown` and the section renderers for intro, snapshot, website text, languages, sentences, geometry, geography, links, methodology, contents, schema, provenance, citation, and hostnames to `card_rendering.py`. Expose typed rendering functions; preserve exact output bytes.

- [ ] **Step 3: Move metadata and byte patching**

Move YAML/front-matter generation, custom-field preservation, derived-field replacement, `yaml_custom_sha256`, and language metadata helpers to `card_metadata.py`. Move heading regexes and byte-level section replacement helpers to `card_patching.py`. Preserve newline style and unrelated sections byte-for-byte.

- [ ] **Step 4: Move release promotion**

Create `publishing/card_artifacts.py` with this typed entry point:

```python
def promote_release_card_artifacts(
    root: Path,
    *,
    source_names: Collection[str] | None,
    summary: PolygonDensitySummary,
    geometry: GeometryStats,
    readme: Path,
    original_readme: bytes,
    updated_readme: bytes,
    yaml_path: Path,
    original_yaml: bytes | None,
    updated_yaml: bytes | None,
) -> None:
    return None
```

Move promotion, map comparison, and staged geometry-report helpers there. `reporting.card` must no longer own release refresh promotion.

- [ ] **Step 5: Keep stable builders and update the release owner**

Keep `build_card` and `update_card_with_geometry` as reporting entry points. Reduce `card.py` to orchestration. Move `refresh_card_for_release` into `publishing`, update `publishing.release`, and keep a tested publishing entry point for callers. Keep `compute_card_stats` and `compute_geometry_stats` in their existing modules.

- [ ] **Step 6: Run focused tests and commit**

Run card, publishing, reporting, and architecture tests; validate language/sentence cards, legacy/custom YAML, stale maps, missing artifacts, global deduplication, and geometry bytes. Then:

```bash
git add src tests
git commit -m "refactor: separate card reporting from release promotion"
```

### Task 5: Preserve deterministic statistics and measured speed (#6, #10)

**Files:**
- Modify: `src/osm_polygon_website_tag/reporting/card_stats.py`
- Modify: `src/osm_polygon_website_tag/reporting/geometry_stats.py`
- Modify: `src/osm_polygon_website_tag/reporting/text_population.py`
- Modify: `src/osm_polygon_website_tag/reporting/geographic/aggregation.py`
- Modify: `src/osm_polygon_website_tag/reporting/geographic/polygon_density.py`
- Test: `tests/reporting/test_card_stats.py`, `tests/reporting/test_geometry_stats.py`, `tests/reporting/test_text_population.py`, `tests/reporting/geographic/test_aggregation.py`, `tests/reporting/geographic/test_models.py`

- [ ] **Step 1: Add byte-stability coverage**

Render the same hermetic fixture twice with different DuckDB thread settings and assert identical text-summary dictionaries, geometry JSON bytes, and map caption values. Keep geometry aggregation serial and allow parallelism only for exact integer/count reducers.

- [ ] **Step 2: Verify the global identity contract**

Use duplicated regional rows with one successful and one pending copy. Assert one canonical `(osm_type, osm_id)` winner, trimmed whitespace-only text excluded, and identical success/word/row counts in card and map summaries.

- [ ] **Step 3: Preserve bounded I/O and spill cleanup**

Keep extracted text out of ranking sorts except for complete-prefix ties, assert the required Parquet column list, and retain spill cleanup in `finally` blocks. Do not materialize the full text population in memory.

- [ ] **Step 4: Run focused tests and commit**

```bash
PYTHONPATH=src /Volumes/Seagate\ M3/projects/osm-polygon-website-tag/repo/.venv/bin/python -m pytest tests/reporting/test_card_stats.py tests/reporting/test_geometry_stats.py tests/reporting/test_text_population.py tests/reporting/geographic/test_aggregation.py tests/reporting/geographic/test_models.py -q
git add src/osm_polygon_website_tag/reporting tests/reporting
git commit -m "perf: preserve deterministic bounded reporting reducers"
```

Record timing for the largest hermetic fixture; add no speculative concurrency.

### Task 6: Split oversized test suites without losing coverage (#11)

**Files:**
- Create: `tests/reporting/test_card_rendering.py`, `tests/reporting/test_card_metadata.py`, `tests/reporting/test_card_patching.py`, `tests/reporting/test_card_release.py`
- Create: `tests/application/test_source_processing_inventory.py`, `tests/application/test_source_processing_enrichment.py`, `tests/application/test_source_processing_resume.py`
- Create: `tests/application/test_workflow_lifecycle.py`, `tests/application/test_workflow_publishing.py`, `tests/application/test_workflow_resume.py`
- Create: `tests/pipeline/test_extraction_records.py` for record-construction cases currently grouped in `test_extraction.py`
- Modify: `tests/reporting/test_card.py`, `tests/application/test_source_processing.py`, `tests/application/test_workflow.py`, `tests/pipeline/test_extraction.py`

- [ ] **Step 1: Record every test name before moving**

```bash
rg -n '^def test_|^    def test_' tests/application/test_source_processing.py tests/application/test_workflow.py tests/reporting/test_card.py tests/pipeline/test_extraction.py > /private/tmp/website-tag-baseline/large-test-names-before.txt
```

- [ ] **Step 2: Centralize repeated setup**

Move repeated run-directory, manifest, Parquet-row, and source-record builders into concern-specific fixture modules. Use pytest `tmp_path`/`tmp_path_factory` only; remove every `tempfile.mkdtemp` occurrence from `tests`.

- [ ] **Step 3: Move tests by concern, preserving names**

Use history-preserving moves for files and apply patches for imports/fixtures. Do not delete or rename any `test_*` function; each original name must occur exactly once after the move.

- [ ] **Step 4: Verify collection identity and size**

```bash
rg -n '^def test_|^    def test_' tests/application tests/reporting tests/pipeline > /private/tmp/website-tag-baseline/test-names-after.txt
sort /private/tmp/website-tag-baseline/large-test-names-before.txt > /private/tmp/website-tag-baseline/before.sorted
sort /private/tmp/website-tag-baseline/test-names-after.txt > /private/tmp/website-tag-baseline/after.sorted
diff -u /private/tmp/website-tag-baseline/before.sorted /private/tmp/website-tag-baseline/after.sorted
find tests -name '*.py' -print0 | xargs -0 wc -l | sort -nr | sed -n '1,12p'
```

Expected: no test-name diff and no moved test file above 600 lines.

- [ ] **Step 5: Run moved suites and commit**

```bash
PYTHONPATH=src /Volumes/Seagate\ M3/projects/osm-polygon-website-tag/repo/.venv/bin/python -m pytest tests/application tests/reporting tests/pipeline -q
git add tests
git commit -m "refactor: split oversized test suites by concern"
```

### Task 7: Audit and remove only proven-stale repository content

**Files:**
- Inspect all tracked files.
- Modify only files proven stale or contradictory, especially `README.md`, `docs/architecture.md`, `docs/data-and-remotes.md`, and `docs/quality/mutation-baseline.txt`.
- Never add generated runs, `.venv`, caches, mutation workspaces, website artifacts, slides, or reports.

- [ ] **Step 1: Build a tracked-file reference inventory**

```bash
git ls-files > /private/tmp/website-tag-baseline/tracked-files.txt
rg -n --glob '!uv.lock' --glob '!*.pyc' 'release-stats|refresh_card_for_release|card_artifacts|mutation_scope|website-tag-release' README.md docs src tests .github justfile pyproject.toml
```

- [ ] **Step 2: Delete only evidence-backed dead content**

For every candidate, require no repository reference and no package/test import. Delete with `apply_patch`, update the nearest documentation when needed, and preserve `CITATION.cff`, `LICENSE`, release receipts, schema docs, and the public release contract.

- [ ] **Step 3: Verify hygiene and documentation**

```bash
git diff --check
uv run --locked mkdocs build --strict
git status --short --ignored | sed -n '1,160p'
```

- [ ] **Step 4: Commit evidence-backed cleanup**

```bash
git add -u
git commit -m "chore: remove stale repository content"
```

### Task 8: Full verification, GitHub reconciliation, and stale-branch cleanup

**Remote objects:** PR #13, issues #6--#12, merged branches `claude/open-prs-review-d69mad` and `claude/polygon-geometry-statistics-ftiojo`, then `codex/website-tag-release` after merge.

- [ ] **Step 1: Run all local repository gates**

```bash
just check
just pre-commit
just pre-push
just qa-gauntlet
uv run --locked mkdocs build --strict
```

Expected: exit code 0 for every command. If a command is blocked by the known native import or unavailable Docker, capture exact output and use the corresponding CI job as the independent gate; do not claim completion.

- [ ] **Step 2: Push and wait for every CI check**

Push the verified branch to PR #13 and run:

```bash
gh pr checks 13 --repo NoeFlandre/osm-polygon-website-tag --watch
```

Expected: quality, Docker smoke, architecture, CRAP, and every scoped mutation shard succeed with no baseline exceptions.

- [ ] **Step 3: Resolve current-head review findings in their original threads**

For every P1/P2 comment, either point to the exact changed code/test and verification job or implement the required correction. Do not dismiss a finding because the selected published run does not exercise its layout.

- [ ] **Step 4: Merge only after green checks and review**

Merge PR #13 only after `mergeable=true`, required checks are successful, and no unresolved review thread remains. Record the merge commit SHA.

- [ ] **Step 5: Close issues with evidence**

Post concise comments on #6--#12 containing the relevant commit SHA, test/CI URLs, and acceptance results, then close only those whose criteria are satisfied.

- [ ] **Step 6: Delete stale branches after merge**

Verify each branch is merged or superseded, then run:

```bash
git push origin --delete claude/open-prs-review-d69mad
git push origin --delete claude/polygon-geometry-statistics-ftiojo
git push origin --delete codex/website-tag-release
```

Keep `main`, `v0.1.0`, the merged commit graph, and PR history. GitHub PR discussion is immutable audit history, not garbage to rewrite.

- [ ] **Step 7: Independently verify the final state**

```bash
git fetch origin --prune
git status --short --branch
git ls-remote --heads origin
gh pr view 13 --repo NoeFlandre/osm-polygon-website-tag --json state,mergedAt,mergeCommit
gh issue list --repo NoeFlandre/osm-polygon-website-tag --state open --limit 100
```

Expected: clean implementation worktree, merged PR, no open issues from #6--#12, and only intentional remote branches. Re-read the Hugging Face revision and confirm the five public metadata files remain unchanged unless a separately verified release update was required.

- [ ] **Step 8: Report the completed evidence**

Record final commit/merge SHAs, all local and CI commands, issue closures, remote deletions, Hugging Face revision, and confirmation that no user-owned untracked artifact in the original checkout was copied or removed.
