# Grid5000 Language Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a tested, offline, resumable Grid'5000 workflow for GlotLID language detection with 30-minute OAR jobs and Seagate as the canonical store.

**Architecture:** Keep language semantics in src/osm_polygon_website_tag/pipeline/detect_languages.py and add a small src/osm_polygon_website_tag/pipeline/grid5000.py bundle boundary for staged source/model identities, receipts, and synchronization. Thin shell scripts submit one bundle at a time to OAR; the reserved-node command calls the existing shard detector with a 25-minute budget.

**Tech Stack:** Python 3.12, PyArrow/Parquet, Typer, FastText, Hugging Face Hub, pytest, Ruff, ty, radon CRAP, mutmut, Bash, rsync, and Grid'5000 OAR.

---

### Task 1: Add time-budgeted shard detection

**Files:**
- Modify: src/osm_polygon_website_tag/pipeline/detect_languages.py
- Modify: tests/pipeline/test_detect_languages.py
- Modify: src/osm_polygon_website_tag/application/cli.py
- Modify: tests/application/test_cli.py

- [ ] **Step 1: Write the failing test**

Add a deterministic clock to the test module and assert that a one-second
budget stops before the second one-row batch, leaves the v1.3 source and one
checkpoint part intact, and returns explicit paused progress.

    def test_time_budget_pauses_between_batches(tmp_path: Path) -> None:
        shard = _write_v1_3_shard(
            tmp_path,
            [_v1_3_text_row(i, website_text=f"English {i}") for i in range(2)],
        )
        clock = iter([0.0, 0.5, 1.1])
        result = detection.detect_language_shard(
            shard,
            detector=RecordingDetector(),
            batch_rows=1,
            time_budget_seconds=1.0,
            clock=lambda: next(clock),
        )
        assert result.completed is False
        assert result.processed_rows == 1
        assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)

- [ ] **Step 2: Run it to verify it fails**

    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py::test_time_budget_pauses_between_batches -q

Expected: FAIL because the detector has no time-budget or clock parameters and
the result has no paused-progress fields.

- [ ] **Step 3: Write the minimal implementation**

Add defaulted completed and processed_rows result fields, validate a positive
optional budget before opening the shard, and check the injected monotonic
clock immediately before each detector batch. On exhaustion, remove only the
staged output and return the original shard hash with completed=False; keep
existing atomic checkpoint writes and exception cleanup unchanged. Add CLI
options --time-budget-seconds and --batch-rows; pass the remaining budget
across sorted shards and do not transition enriching to enriched after a
paused shard.

- [ ] **Step 4: Run targeted tests**

    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py tests/application/test_cli.py -q

Expected: all existing language and CLI tests plus the new red/green cases
pass.

- [ ] **Step 5: Commit**

    git add src/osm_polygon_website_tag/pipeline/detect_languages.py tests/pipeline/test_detect_languages.py src/osm_polygon_website_tag/application/cli.py tests/application/test_cli.py
    git commit -m "feat: add resumable language detection time budgets"

### Task 2: Add offline model loading and bundle contracts

**Files:**
- Modify: src/osm_polygon_website_tag/pipeline/glotlid.py
- Create: src/osm_polygon_website_tag/pipeline/grid5000.py
- Create: tests/pipeline/test_grid5000.py
- Modify: src/osm_polygon_website_tag/pipeline/README.md

- [ ] **Step 1: Write the failing tests**

Test that a local model path produces the pinned identity, that preparation
copies exactly one selected shard and model into a bundle, and that the
manifest records the source row count, commit, and SHA-256 values.

    def test_prepare_bundle_records_source_and_model_identity(tmp_path: Path) -> None:
        run_dir = _write_enriched_run(tmp_path)
        model = tmp_path / "model_v3.bin"
        model.write_bytes(b"model")
        manifest = prepare_language_bundle(
            run_dir,
            tmp_path / "bundle",
            model_path=model,
            commit="abc123",
        )
        assert manifest.source_shard == "source.parquet"
        assert manifest.model.sha256 == hashlib.sha256(b"model").hexdigest()
        assert (tmp_path / "bundle" / "model_v3.bin").read_bytes() == b"model"

- [ ] **Step 2: Run it to verify it fails**

    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_grid5000.py -q

Expected: FAIL because offline loading and bundle preparation do not exist.

- [ ] **Step 3: Write the minimal implementation**

Add load_glotlid_detector_from_path(model_path), which hashes and loads a
staged binary without calling Hugging Face. Define validated Grid5000Bundle
and Grid5000Result dataclasses, safe basename checks, bounded SHA-256 copying,
atomic JSON writes, and prepare_language_bundle(run_dir, bundle_dir,
model_path, commit). Select the first unfinished shard in stable order, copy
its valid checkpoint prefix, and bind the bundle to source and model identity.

- [ ] **Step 4: Run targeted tests and refactor**

    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_grid5000.py tests/pipeline/test_glotlid.py -q
    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked ruff check src/osm_polygon_website_tag/pipeline tests/pipeline

Expected: tests and Ruff pass; after they are green, keep validation separate
from copying and remove duplication discovered by the refactor.

- [ ] **Step 5: Commit**

    git add src/osm_polygon_website_tag/pipeline/glotlid.py src/osm_polygon_website_tag/pipeline/grid5000.py src/osm_polygon_website_tag/pipeline/README.md tests/pipeline/test_grid5000.py
    git commit -m "feat: add offline Grid5000 language bundles"

### Task 3: Run and synchronize one bundle

**Files:**
- Modify: src/osm_polygon_website_tag/pipeline/grid5000.py
- Modify: src/osm_polygon_website_tag/application/cli.py
- Modify: tests/pipeline/test_grid5000.py
- Modify: tests/application/test_cli.py

- [ ] **Step 1: Write the failing tests**

Test a paused result that installs only the checkpoint and leaves the source
byte-identical, and a completed result that atomically replaces the source
shard, updates its public hash, and transitions the run to enriched when all
shards are complete.

    def test_sync_paused_bundle_preserves_source_and_installs_checkpoint(tmp_path: Path) -> None:
        run_dir, bundle = _prepared_bundle(tmp_path)
        original = (run_dir / "polygons" / "source.parquet").read_bytes()
        _write_paused_result(bundle)
        sync_language_bundle(bundle, run_dir)
        assert (run_dir / "polygons" / "source.parquet").read_bytes() == original
        assert (run_dir / "polygons" / ".source.parquet.language.parts" / "part-00000000.parquet").is_file()

- [ ] **Step 2: Run it to verify it fails**

    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_grid5000.py::test_sync_paused_bundle_preserves_source_and_installs_checkpoint -q

Expected: FAIL because bundle execution and synchronization are not exposed.

- [ ] **Step 3: Write the minimal implementation**

Add run_language_bundle(bundle_dir, time_budget_seconds, batch_rows) with
offline model loading, the existing detector, and an atomic result receipt.
Add sync_language_bundle(bundle_dir, run_dir) with source/model/result identity
checks, atomic shard and checkpoint installation, run-manifest updates, and a
Seagate manifests/grid5000 history receipt. Expose grid5000-prepare,
grid5000-run, and grid5000-sync Typer commands. Prepare and sync require
Seagate paths; the reserved-node run command accepts only a validated bundle
and never downloads or fetches network data.

- [ ] **Step 4: Run targeted integration tests**

    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_grid5000.py tests/application/test_cli.py -q

Expected: all bundle, CLI, frozen-run, malformed-receipt, and compatibility
tests pass.

- [ ] **Step 5: Commit**

    git add src/osm_polygon_website_tag/pipeline/grid5000.py src/osm_polygon_website_tag/application/cli.py tests/pipeline/test_grid5000.py tests/application/test_cli.py
    git commit -m "feat: run and synchronize Grid5000 language jobs"

### Task 4: Add OAR scripts and policy-aware operations

**Files:**
- Create: scripts/grid5000/prepare_language_detection.sh
- Create: scripts/grid5000/submit_language_detection.sh
- Create: scripts/grid5000/run_language_detection.sh
- Create: scripts/grid5000/sync_language_detection.sh
- Create: scripts/grid5000/README.md
- Modify: docs/operations.md
- Modify: docs/setup.md
- Modify: README.md
- Create: tests/architecture/test_grid5000.py

- [ ] **Step 1: Write the failing tests**

Add architecture checks for executable scripts, walltime=0:30, a 25-minute
default budget, bounded CPU resources, no GPU requirement,
usagepolicycheck -t before and after oarsub, no token literals, and offline
staged execution.

    def test_submit_script_checks_policy_around_submission() -> None:
        script = Path("scripts/grid5000/submit_language_detection.sh").read_text()
        assert script.index("usagepolicycheck -t") < script.index("oarsub")
        assert script.rindex("usagepolicycheck -t") > script.index("oarsub")
        assert "walltime=0:30" in script
        assert "host=1/core=" in script

- [ ] **Step 2: Run it to verify it fails**

    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/architecture/test_grid5000.py -q

Expected: FAIL because the Grid5000 scripts and architecture tests do not yet
exist.

- [ ] **Step 3: Write the minimal implementation**

Add a Seagate prepare wrapper, a frontend-only submit wrapper with a single
active-job marker, and an OAR runner that invokes uv run --locked --offline
grid5000-run with --time-budget-seconds 1500. Request
host=1/core=2,walltime=0:30; leave queue and site properties configurable.
Add an explicit sync wrapper. Document the sequence: policy check, pinned
checkout, Seagate preparation, rsync to the site, one submission, oarstat
monitoring, rsync back, checksum verification, and confirmed remote cleanup.

- [ ] **Step 4: Run script and documentation tests**

    for script in scripts/grid5000/*.sh; do bash -n "$script"; done
    UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/architecture/test_grid5000.py tests/application/test_docs_site.py -q

Expected: shell syntax and documentation/architecture tests pass.

- [ ] **Step 5: Commit**

    git add scripts/grid5000 docs/operations.md docs/setup.md README.md tests/architecture/test_grid5000.py
    git commit -m "docs: add policy-aware Grid5000 job workflow"

### Task 5: Download the model and run the quality gates

**Files:**
- No generated model/cache files are committed.
- Model/cache artifacts remain under /Volumes/Seagate M3/projects/osm-polygon-website-tag/.

- [ ] **Step 1: Download and verify the pinned model**

    hf download cis-lmu/glotlid model_v3.bin \
      --revision 85cd671 \
      --cache-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid'

Use the model identity helper against the downloaded binary and record only
its path, revision, and SHA-256 in the Seagate bundle manifest.

- [ ] **Step 2: Run the repository gates**

    just check
    just pre-commit
    just pre-push
    just crap
    just mutation

Expected: every command exits zero; mutation output contains no survived,
untested, timeout, suspicious, interrupted, or segfaulted mutants; CRAP is
below 6 for every reported function.

- [ ] **Step 3: Verify exact scope and readiness**

    git diff --check
    git status --short --branch
    git diff --name-only HEAD~1..HEAD

Confirm the model cache is outside Git on Seagate and the Grid5000 README has
no credentials or site-specific assumptions.

- [ ] **Step 4: Commit validated gate fixes**

    git add src/osm_polygon_website_tag/pipeline/detect_languages.py src/osm_polygon_website_tag/pipeline/glotlid.py src/osm_polygon_website_tag/pipeline/grid5000.py src/osm_polygon_website_tag/application/cli.py tests/pipeline/test_detect_languages.py tests/pipeline/test_glotlid.py tests/pipeline/test_grid5000.py tests/application/test_cli.py tests/architecture/test_grid5000.py scripts/grid5000 docs/operations.md docs/setup.md README.md src/osm_polygon_website_tag/pipeline/README.md
    git commit -m "fix: close Grid5000 quality-gate findings"

- [ ] **Step 5: Push and report readiness**

    git push origin main
    git rev-parse HEAD
    git ls-remote origin refs/heads/main

Report the final commit, model path/hash, quality-gate results, and the runtime
values still required from the operator: Grid5000 site/frontend, account, and
remote staging path.
