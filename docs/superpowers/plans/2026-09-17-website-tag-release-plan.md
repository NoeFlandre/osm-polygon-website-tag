# Website-tag release contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended). Steps use checkbox ( - [ ] ) syntax for tracking.

**Goal:** Make the website-tag release deterministic and auditable from regional Parquet observations through the card, map, geometry report, remote Hugging Face files, and final no-op verification.

**Architecture:** Add one spillable DuckDB-backed text-population reducer that selects qualifying identities and canonical rows using the repository's documented winner order. The map, card, YAML, and release verifier consume that reducer; geometry statistics remain explicitly scoped to published polygon rows. Preserve legacy card sections and receipts while strengthening new receipts and remote inventory checks.

**Tech Stack:** Python 3.12, uv, PyArrow, DuckDB, H3, Matplotlib, pytest, Ruff, ty, mutmut, Just, Docker, GitHub CLI, Hugging Face Hub CLI.

---

### Task 1: Capture the RED global text-population contract

**Files:**
- Create: src/osm_polygon_website_tag/reporting/text_population.py
- Create: tests/reporting/test_text_population.py
- Modify: tests/reporting/geographic/test_aggregation.py
- Modify: tests/reporting/geographic/test_rendering.py
- Modify: tests/reporting/test_card_stats.py

- [ ] **Step 1: Write failing tests.**

Create two regional Parquet copies of one identity with different coordinates and text plus explicit osm_version, osm_timestamp, source_pbf, and polygon_id. Assert that winner order is highest version, newest timestamp, smallest source PBF, then smallest polygon ID. Reverse file and row order and assert identical selected coordinates, text, counts, words, and H3 cells. Assert failed/blank text is excluded, website and contact identities are counted once independently, and a legacy shard without text-status columns is ignored.

Use this interface in the tests:

    population = compute_text_population_summary(run_dir)
    assert population.unique_identity_count == 1
    assert population.website_identity_count == 1
    assert population.contact_website_identity_count == 0
    assert list(iter_canonical_text_coordinates(run_dir)) == [
        TextCoordinate(osm_type="way", osm_id=42, lat=48.85, lon=2.35, source_path=...)
    ]

Add mode tests for regional_rows and global_unique_text. The global caption must say that regional overlap duplicates were removed globally; the regional caption must say regional polygon rows/centroids and never unique polygons. Add a card-stat test proving tag counts and words do not repeat regional copies.

- [ ] **Step 2: Run the focused tests and confirm RED.**

    UV_CACHE_DIR=/tmp/osm-website-tag-uv-cache uv run --locked pytest \
      tests/reporting/test_text_population.py \
      tests/reporting/geographic/test_aggregation.py \
      tests/reporting/geographic/test_rendering.py \
      tests/reporting/test_card_stats.py -q

Expected: failures for the missing reducer, explicit mode, canonical winner, caption, and global-count behavior.

- [ ] **Step 3: Commit the RED tests.**

    git add tests/reporting/test_text_population.py tests/reporting/geographic/test_aggregation.py tests/reporting/geographic/test_rendering.py tests/reporting/test_card_stats.py
    git commit -m "test: define global website text population contract"

### Task 2: Implement the bounded deterministic reducer and map modes

**Files:**
- Modify: src/osm_polygon_website_tag/reporting/text_population.py
- Modify: src/osm_polygon_website_tag/reporting/geographic/inputs.py
- Modify: src/osm_polygon_website_tag/reporting/geographic/aggregation.py
- Modify: src/osm_polygon_website_tag/reporting/geographic/models.py
- Modify: src/osm_polygon_website_tag/reporting/geographic/polygon_density.py

- [ ] **Step 1: Implement one DuckDB-backed reducer.**

Use fresh_connection(run_dir) and run-owned spill storage. Select regional polygon shards when analysis_observations is a symlink; otherwise select public shards. Require lat, lon, osm_type, osm_id, both text values, and both text statuses; skip files that cannot prove the contract. Define a qualifying field as status success and trimmed text non-empty.

Create all_rows, qualified_rows, canonical_any, canonical_website, and canonical_contact views. Every row-number window uses:

    ORDER BY osm_version DESC NULLS LAST,
             osm_timestamp DESC NULLS LAST,
             source_pbf ASC NULLS LAST,
             polygon_id ASC NULLS LAST,
             lat ASC NULLS LAST,
             lon ASC NULLS LAST,
             website_text ASC NULLS LAST,
             contact_website_text ASC NULLS LAST

canonical_any selects one qualifying geometry/payload per identity. The per-tag views select one qualifying text per identity, so website and contact counts and word totals are each unique. Iterate coordinates in Arrow batches of 8,192 and aggregate counts, URL presence, words, and language totals without materializing the dataset in Python.

Expose frozen TextCoordinate and TextPopulationSummary dataclasses, compute_text_population_summary, and iter_canonical_text_coordinates. Keep iter_unique_text_lat_lon_runs as a compatibility wrapper that delegates to the reducer.

- [ ] **Step 2: Add explicit aggregation modes.**

Add AggregationMode = Literal["regional_rows", "global_unique_text"] and an aggregation_mode keyword to compute_polygon_density_summary and build_polygon_density_map. Keep extracted_text_only as a compatibility alias and reject contradictory values. Store the resolved mode on PolygonDensitySummary while preserving its extracted_text_only property.

- [ ] **Step 3: Use canonical coordinates and render truthful captions.**

regional_rows uses the existing bounded row iterator. global_unique_text uses iter_canonical_text_coordinates and an optional supplied TextPopulationSummary. The global caption says “unique polygons with successful non-empty website or contact:website text; regional overlap duplicates removed globally”; the regional caption says “regional polygon rows/centroids”. Include population scope in PolygonDensityRenderResult.

- [ ] **Step 4: Run GREEN focused tests and commit.**

    UV_CACHE_DIR=/tmp/osm-website-tag-uv-cache uv run --locked pytest \
      tests/reporting/test_text_population.py \
      tests/reporting/geographic/test_aggregation.py \
      tests/reporting/geographic/test_rendering.py -q

Expected: all reducer, ordering, mode, coordinate, and caption tests pass.

    git add src/osm_polygon_website_tag/reporting/text_population.py src/osm_polygon_website_tag/reporting/geographic/inputs.py src/osm_polygon_website_tag/reporting/geographic/aggregation.py src/osm_polygon_website_tag/reporting/geographic/models.py src/osm_polygon_website_tag/reporting/geographic/polygon_density.py tests/reporting
    git commit -m "feat: canonicalize global website text populations"

### Task 3: Align card, YAML, map, geometry report, and verification

**Files:**
- Modify: src/osm_polygon_website_tag/reporting/card_stats.py
- Modify: src/osm_polygon_website_tag/reporting/card.py
- Modify: src/osm_polygon_website_tag/reporting/geometry_stats.py
- Modify: src/osm_polygon_website_tag/reporting/verification/analysis.py
- Modify: tests/reporting/test_card.py
- Modify: tests/reporting/test_card_stats.py
- Modify: tests/reporting/test_geometry_stats.py
- Modify: tests/reporting/test_verification_private.py

- [ ] **Step 1: Add failing golden assertions.**

Require distinct fields for regional observations, published rows, globally unique text identities, website identities, contact identities, words, duplicates, rejected rows, geometry-row count, and area totals. Require the global caption in README and map. Require stats.json to label its geometry population as published polygon rows. Add a golden test that custom legacy README sections and existing YAML fields remain byte-for-byte unchanged during release refresh.

- [ ] **Step 2: Thread one population summary through card generation.**

In build_card and refresh_card_for_release, compute one TextPopulationSummary, pass it to the global H3 summary, pass both summaries to compute_card_stats, and render the map from that same H3 summary. Replace repeated regional text totals in the public text table with deduplicated tag totals; retain clearly labeled regional observation totals. Derive language totals from canonical text winners. Keep sentence-table metrics explicitly labeled as analysis outputs.

- [ ] **Step 3: Make geometry scope explicit.**

Add population_scope = "published polygon rows" to GeometryStats. Keep all geometry, area, shape, extent, source, and tag calculations over selected public polygon rows. Render that scope in README and stats.json without merging it with the unique-text population.

- [ ] **Step 4: Strengthen normal and release verification.**

Recompute text population, global H3 summary, card stats, and geometry report in verification. Compare README, YAML, stats.json, and map presence; verify the global caption and geometry population scope. Preserve all non-derived legacy README sections.

- [ ] **Step 5: Run tests and commit.**

    UV_CACHE_DIR=/tmp/osm-website-tag-uv-cache uv run --locked pytest \
      tests/reporting/test_card.py tests/reporting/test_card_stats.py \
      tests/reporting/test_geometry_stats.py tests/reporting/test_verification_private.py -q

Expected: card, geometry, golden, preservation, and verifier tests pass.

    git add src/osm_polygon_website_tag/reporting/card_stats.py src/osm_polygon_website_tag/reporting/card.py src/osm_polygon_website_tag/reporting/geometry_stats.py src/osm_polygon_website_tag/reporting/verification/analysis.py tests/reporting
    git commit -m "feat: align card map and geometry populations"

### Task 4: Harden legacy receipts and remote identity verification

**Files:**
- Modify: src/osm_polygon_website_tag/reporting/verification/receipt.py
- Modify: src/osm_polygon_website_tag/publishing/release.py
- Modify: tests/reporting/test_verification_private.py
- Modify: tests/publishing/test_release.py

- [ ] **Step 1: Add failing tests.**

Create a valid recognized legacy receipt without data_manifest_sha256 and assert local verification accepts it when all receipt-bound files and the legacy schema are valid. Assert new receipts require the field. Mock unchanged receipts with changed Parquet bytes, changed sources.json, and changed expected_sources.json; each must fail. Assert missing and unexpected remote Parquet files fail inventory verification.

- [ ] **Step 2: Implement the compatibility boundary.**

Accept a missing data_manifest_sha256 only for the recognized legacy schema/version emitted by this repository. Require it on new receipts. Keep artifact digests and inventory checks active in both paths. Independently enumerate remote Parquet files, manifests, sizes, and content/LFS hashes; compare a recomputed remote identity with the local release plan instead of trusting the remote receipt.

- [ ] **Step 3: Preserve exact release scope and idempotence.**

Keep README.md, dataset.yaml, stats.json, the map, and completion receipt in the release plan. Verify non-refreshable receipt artifacts before card refresh. Replace the receipt atomically when card-bound bytes change. Require the second identical release command to report no_op=true and changed_files=[].

- [ ] **Step 4: Run tests and commit.**

    UV_CACHE_DIR=/tmp/osm-website-tag-uv-cache uv run --locked pytest tests/publishing/test_release.py tests/reporting/test_verification_private.py -q

Expected: legacy, changed-Parquet, changed-manifest, inventory, and no-op tests pass.

    git add src/osm_polygon_website_tag/reporting/verification/receipt.py src/osm_polygon_website_tag/publishing/release.py tests/publishing/test_release.py tests/reporting/test_verification_private.py
    git commit -m "fix: independently verify legacy and remote release identity"

### Task 5: Fix mutation scope and fail closed on empty mutation work

**Files:**
- Modify: scripts/quality/mutation_scope.py
- Modify: scripts/quality/mutation_runner.py
- Modify: scripts/quality/mutation_gate.py
- Modify: tests/quality/test_mutation_scope.py
- Modify: tests/quality/test_mutation_runner.py
- Modify: tests/quality/test_mutation_gate.py

- [ ] **Step 1: Add RED initializer and empty-work tests.**

Assert:

    module_filters(["src/osm_polygon_website_tag/reporting/geographic/__init__.py"])
    == ["osm_polygon_website_tag.reporting.geographic.*"]
    module_filters(["src/osm_polygon_website_tag/__init__.py"])
    == ["osm_polygon_website_tag.*"]
    module_filters(["src/osm_polygon_website_tag/reporting/geographic/aggregation.py"])
    == ["osm_polygon_website_tag.reporting.geographic.aggregation.*"]

Add runner tests for those source paths and gate tests that zero generated mutants or zero associated tests exits nonzero with an actionable message.

- [ ] **Step 2: Implement exact mapping and guards.**

Strip __init__ only when converting a source path to its package module; preserve ordinary nested module names. Fail before mutmut when a requested filter generates no mutants, and fail after stats collection when the requested scope has no associated tests. Make the gate reject an explicitly empty result for a non-empty requested scope, while allowing the CI-only empty scope flag. Keep no tests, survived, timeout, crash, and interruption verdicts unverified.

- [ ] **Step 3: Run tests and commit.**

    UV_CACHE_DIR=/tmp/osm-website-tag-uv-cache uv run --locked pytest tests/quality/test_mutation_scope.py tests/quality/test_mutation_runner.py tests/quality/test_mutation_gate.py -q

Expected: initializer mapping, ordinary mapping, no-mutant, no-test, and baseline-aware gate tests pass.

    git add scripts/quality/mutation_scope.py scripts/quality/mutation_runner.py scripts/quality/mutation_gate.py tests/quality
    git commit -m "fix: make mutation scope and empty runs fail closed"

### Task 6: Run all local gates and independent review

- [ ] **Step 1: Run the exact required checks.**

Use MPLCONFIGDIR=/Volumes/Seagate M3/projects/osm-polygon-website-tag/cache/mplconfig, MPLBACKEND=Agg, and a task-scoped UV_CACHE_DIR:

    just baseline
    just ruff
    just typecheck
    just unit
    just acceptance
    just architecture
    just crap
    just mutation-scope origin/main
    just mutation
    just smoke
    just diff-review
    just pre-commit
    just pre-push
    UV_CACHE_DIR=/tmp/osm-website-tag-uv-cache uv run --locked mkdocs build --strict

Run pytest tests/property -q when that directory exists. Record exact test totals, CRAP output, mutation generated/killed/survived/no-test counts, and Docker result.

- [ ] **Step 2: Inspect and independently review.**

Use git diff origin/main...HEAD and git status --short. Run the supervised code-review workflow and require review of identity/canonicalization, memory bounds, README preservation, receipt compatibility, remote verification, mutation mapping, tests, and release scope. Resolve actionable findings and rerun affected checks before opening the PR.

- [ ] **Step 3: Verify clean scope.**

    git status --short --branch
    git diff --check origin/main...HEAD

Expected: only intended tracked changes exist; reports/ and slides/ remain untracked and untouched.

### Task 7: Open, merge, and update GitHub state

- [ ] **Step 1: Recheck remote state.**

Confirm PR #4 remains merged at 7373281, inspect issues #3, #6, #7, #8, and #9, and create one focused PR from codex/website-tag-release with a Conventional Commit title and evidence-rich body.

- [ ] **Step 2: Push, wait for checks, review, and merge.**

Merge only after required checks pass. Fetch origin/main and record the merge commit. Do not mark issues Done yet.

- [ ] **Step 3: Update the project only after evidence.**

Use In review before merge and Done only after merged code, local gates, remote publication, independent HF verification, and the second no-op release all pass. Leave incomplete issues In progress or In review with evidence.

### Task 8: Execute and independently verify the HDD release

- [ ] **Step 1: Select the run.**

Inventory candidate metadata, receipts, source manifests, Parquet shards, analysis tables, map, README, and disk space. Select only the complete validated glotlid run matching the target release. Reject the extracting canonical run and partial or stale runs.

- [ ] **Step 2: Verify and dry-run.**

    uv run --locked osm-polygon-website-tag verify-results --run-dir "<RUN_DIR>"
    uv run --locked osm-polygon-website-tag publish-plan --run-dir "<RUN_DIR>" --repo-id NoeFlandre/osm-polygon-website-tag
    uv run --locked osm-polygon-website-tag release-stats --run-dir "<RUN_DIR>" --confirm-repo NoeFlandre/osm-polygon-website-tag

Inspect the plan and require only intended card, YAML, stats, map, receipt, analysis, polygon, rejection, and manifest artifacts.

- [ ] **Step 3: Apply once after merge and review.**

Run the authorized release-stats --apply command once. Capture HF revision, remote inventory, Parquet sizes/hashes, manifest hashes, receipt, counts, areas, map hash, schema/row checks, and Dataset Viewer response. Recompute remote identity independently.

- [ ] **Step 4: Prove no-op idempotence.**

Run the exact same release command again and require no_op=true and changed_files=[]. Recheck revision and hashes. If any remote file differs, report the path and mismatch rather than claiming completion.

- [ ] **Step 5: Handoff.**

Report issue/PR statuses, branch and merge commits, every gate result, selected run identity, inventories, regional/published/unique/rejected counts, geometry area summary, map hash, HF revision, Viewer result, second-run no-op, and remaining risks. Use absolute local paths and never claim an unchecked result.

## Plan self-review

- Spec coverage: Tasks 1–3 cover identity, canonical selection, captions, card/map/report agreement, geometry, and README preservation. Task 4 covers legacy receipts, remote Parquet/manifests, changed-remote detection, and no-op release. Task 5 covers nested initializer scope and zero-mutant/no-test guards. Tasks 6–8 cover quality, review, merge, project, publication, and independent verification.
- Placeholder scan: the only angle-bracket value is the runtime argument RUN_DIR, resolved from the verified HDD inventory in Task 8.
- Type consistency: AggregationMode, TextCoordinate, TextPopulationSummary, and compute/iterator interfaces are introduced in Task 2 and consumed in Tasks 1 and 3; GeometryStats remains a separate population.
