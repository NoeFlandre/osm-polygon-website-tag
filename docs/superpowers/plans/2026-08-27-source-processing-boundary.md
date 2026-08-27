# Source Processing Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the per-source transaction from `application/workflow.py` into a typed deep module without changing public behavior, artifacts, checkpoints, or run-state semantics.

**Architecture:** `application.workflow` remains the run-level state machine. A new `application.source_processing` module owns one source's extraction, migration, enrichment, optional language detection, incremental publication, and source-level metadata updates behind `process_sources()`. The public `run_all()` signature and `WorkflowResult` remain unchanged.

**Tech Stack:** Python 3.12, `uv`, pytest, Ruff, ty, PyArrow/Parquet, existing run-state and publishing checkpoints.

---

## File map

- Create: `src/osm_polygon_website_tag/application/source_processing.py` — typed source-processing context, phase counts, and private per-source transaction helpers.
- Modify: `src/osm_polygon_website_tag/application/workflow.py` — retain run orchestration and delegate source phases to the new module.
- Modify: `src/osm_polygon_website_tag/application/README.md` — document the source-processing boundary.
- Modify: `tests/reporting/test_verification_private.py` — align the existing test double with the current public-schema helper; production behavior is unchanged.
- Create: `tests/application/test_source_processing.py` — boundary and source-processing characterization tests.
- Modify: `tests/application/test_workflow.py` — move source-transaction helper coverage to the new module and retain run-level orchestration coverage.

## Task 1: Establish a green current-tree baseline

**Files:** `tests/reporting/test_verification_private.py`

- [ ] **Step 1: Reproduce the existing baseline failure.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/reporting/test_verification_private.py::test_shard_helpers_cover_valid_and_invalid_inventory_branches -q
```

Expected: fail because the fake schema object reaches `is_current_public_polygon_schema()` while the test currently patches only `shards.schema_matches`.

- [ ] **Step 2: Repair only the test seam.**

In `test_shard_helpers_cover_valid_and_invalid_inventory_branches`, patch the helper actually used by the public-shard branch:

```python
monkeypatch.setattr(shards, "is_current_public_polygon_schema", lambda *_args: True)
# Before the later invalid-schema assertion, replace the return value:
monkeypatch.setattr(shards, "is_current_public_polygon_schema", lambda *_args: False)
```

Keep the existing `schema_matches` patch for comparison and rejection contracts. Do not change `reporting/verification/shards.py`.

- [ ] **Step 3: Verify the repaired baseline test.**

Run the same focused pytest command. Expected: pass.

- [ ] **Step 4: Commit the isolated test-seam repair.**

```bash
git add tests/reporting/test_verification_private.py
git commit -m "test: align shard verification schema seam"
```

## Task 2: Add the source-processing boundary test first

**Files:** `tests/application/test_source_processing.py`

- [ ] **Step 1: Write the failing boundary test.**

Create a test that expresses the narrow facade and does not depend on workflow internals:

```python
from pathlib import Path
from types import SimpleNamespace

from osm_polygon_website_tag.application import source_processing
from osm_polygon_website_tag.application.source_processing import (
    SourceProcessingContext,
)
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
from osm_polygon_website_tag.runtime.run_state import RunState, SourceFingerprint


def test_process_sources_returns_counts_in_order(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "first.osm.pbf"
    second = tmp_path / "second.osm.pbf"
    calls: list[tuple[str, int, int, bool]] = []

    def process_source(**kwargs: object) -> SimpleNamespace:
        source = kwargs["source"]
        index = kwargs["index"]
        total = kwargs["total"]
        allow_extraction = kwargs["allow_extraction"]
        assert isinstance(source, Path)
        assert isinstance(index, int)
        assert isinstance(total, int)
        assert isinstance(allow_extraction, bool)
        calls.append((source.name, index, total, allow_extraction))
        return SimpleNamespace(extracted=index == 1, reused=index == 2, uploaded=True)

    context = SourceProcessingContext(
        run_dir=tmp_path,
        state=RunState(run_dir=tmp_path, run_id="test"),
        repo_id="owner/dataset",
        apply=False,
        progress=None,
        invocation_id="test",
        upload_checkpoint=CheckpointV2(
            schema_version="v2", global_bundle={}, sources={}
        ),
        area_workers=None,
        max_in_flight_areas=None,
        fetch_workers=None,
        detect_languages=False,
        language_detector=None,
    )
    monkeypatch.setattr(source_processing, "_process_source", process_source, raising=False)
    result = source_processing.process_sources(
        sources=[first, second],
        ordered_sources=[second, first],
        fingerprints_by_name={
            "first.osm.pbf": SourceFingerprint("first.osm.pbf", 0, 0),
            "second.osm.pbf": SourceFingerprint("second.osm.pbf", 0, 0),
        },
        context=context,
        allow_extraction=False,
    )

    assert calls == [
        ("second.osm.pbf", 1, 2, False),
        ("first.osm.pbf", 2, 2, False),
    ]
    assert result.extracted == 1
    assert result.reused == 1
    assert result.uploaded == 2
```

The test imports the intended API before it exists, so collection fails in the
RED step. `SimpleNamespace` is used only for the private transaction result
because the facade test observes the three result attributes and the concrete
transaction type is intentionally private to the source-processing module.

- [ ] **Step 2: Run the new test and confirm RED.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_source_processing.py::test_process_sources_returns_counts_in_order -q
```

Expected: collection failure stating that `application.source_processing` is not available.

- [ ] **Step 3: Commit the RED test.**

```bash
git add tests/application/test_source_processing.py
git commit -m "test: define source processing boundary"
```

## Task 3: Create the typed deep module and make the boundary GREEN

**Files:** `src/osm_polygon_website_tag/application/source_processing.py`, `tests/application/test_source_processing.py`

- [ ] **Step 1: Add the minimal typed facade.**

Define the public types and facade with no new dependencies:

```python
@dataclass(frozen=True)
class SourceProcessingContext:
    run_dir: Path
    state: RunState
    repo_id: str
    apply: bool
    progress: Callable[[str], None] | None
    invocation_id: str
    upload_checkpoint: CheckpointV2
    area_workers: int | None
    max_in_flight_areas: int | None
    fetch_workers: int | None
    detect_languages: bool
    language_detector: LanguageDetector | None


@dataclass(frozen=True)
class SourcePhaseCounts:
    extracted: int = 0
    reused: int = 0
    uploaded: int = 0


def process_sources(
    *,
    sources: Sequence[Path],
    ordered_sources: Sequence[Path],
    fingerprints_by_name: Mapping[str, SourceFingerprint],
    context: SourceProcessingContext,
    allow_extraction: bool,
) -> SourcePhaseCounts:
    counts = SourcePhaseCounts()
    for index, source in enumerate(ordered_sources, start=1):
        result = _process_source(
            source=source,
            fingerprint=fingerprints_by_name[source.name],
            context=context,
            index=index,
            total=len(sources),
            allow_extraction=allow_extraction,
        )
        counts = SourcePhaseCounts(
            extracted=counts.extracted + int(result.extracted),
            reused=counts.reused + int(result.reused),
            uploaded=counts.uploaded + int(result.uploaded),
        )
    return counts
```

The initial implementation may use a private `_SourceTransactionResult` and the existing source-processing helper body moved in Task 4. Keep `__all__` limited to `SourcePhaseCounts`, `SourceProcessingContext`, and `process_sources`.

- [ ] **Step 2: Verify the typed fixture is ready for the GREEN step.**

Confirm the test constructs `SourceProcessingContext`, `RunState`, `CheckpointV2`,
and `SourceFingerprint` values exactly as specified in the fixture block; no
untyped context or fake fingerprint remains.

- [ ] **Step 3: Run the focused test and confirm GREEN.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_source_processing.py::test_process_sources_returns_counts_in_order -q
```

Expected: pass.

## Task 4: Move the cohesive per-source transaction behind the facade

**Files:** `src/osm_polygon_website_tag/application/source_processing.py`, `src/osm_polygon_website_tag/application/workflow.py`

- [ ] **Step 1: Move the source-processing dataclasses and kwargs types.**

Move `_SourceTransactionResult`, `_SourceBundleResult`, `_EnrichmentDecision`, `_ExtractionKwargs`, and `_EnrichmentKwargs` from `workflow.py` to the new module. Rename only the context and phase-count types to the public names from Task 3; retain field names and defaults.

- [ ] **Step 2: Move the per-source helpers without changing their bodies.**

Move these functions into `source_processing.py` and preserve their call order and signatures unless a typed context parameter replaces the old private context type:

```text
_process_source
_ensure_source_bundle
_extract_with_options
_migrate_public_shard_if_needed
_enrich_source_shard_if_needed
_detect_source_shard_if_needed
_publish_source_if_needed
_source_upload_is_current_for_context
_source_requires_publication
_record_source_upload
_published_source_names
_source_upload_is_current
_public_shard_path
_upload_public_shard
_maybe_publish_enriched_shard
_shard_needs_enrichment
_schema_needs_enrichment
_status_columns_need_enrichment
_run_needs_enrichment
_run_needs_language_detection
```

Retain the existing imports and all error messages, progress messages, atomic promotions, checkpoint updates, detector checks, and `KeyboardInterrupt` behavior. Do not introduce workflow imports into the new module; it may import lower layers exactly as the current workflow does.

- [ ] **Step 3: Move source-batch accumulation into `process_sources()`.**

Delete `_process_source_batch` and `_process_source` implementations from `workflow.py`. Do not leave compatibility wrappers. Keep `_add_phase_counts` in `workflow.py` only if it still combines extraction and enrichment results; otherwise use a small typed helper in the new module.

- [ ] **Step 4: Run the source-processing tests.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_source_processing.py tests/pipeline/test_enrich.py tests/pipeline/test_detect_languages.py tests/pipeline/test_language_detection_checkpoint.py -q
```

Expected: pass with no changed behavior.

## Task 5: Rewire the run orchestrator and migrate tests

**Files:** `src/osm_polygon_website_tag/application/workflow.py`, `tests/application/test_workflow.py`, `tests/application/test_source_processing.py`, `src/osm_polygon_website_tag/application/README.md`

- [ ] **Step 1: Construct the new context in `run_all()`.**

Replace the local `_SourceRunContext` construction with `SourceProcessingContext` and keep every field value identical. `run_all()` continues to own setup, resume ordering, status transitions, analysis, card generation, finalization, and complete-run publication.

- [ ] **Step 2: Delegate both source phases.**

Change `_run_extraction_phase()` and `_run_enrichment_phase()` to call:

```python
process_sources(
    sources=sources,
    ordered_sources=ordered_sources,
    fingerprints_by_name=fingerprints_by_name,
    context=context,
    allow_extraction=True,  # False in the enrichment phase
)
```

Preserve the existing status transition sequence and count accumulation.

- [ ] **Step 3: Move helper-focused tests to the new module.**

Update imports and monkeypatch targets in `test_workflow.py` so source-transaction assertions live in `test_source_processing.py`. Keep run-level tests in `test_workflow.py`: default and opt-in `run_all`, resume behavior, frozen snapshots, status transitions, and finalization. Do not weaken assertions to accommodate the move.

- [ ] **Step 4: Update the application boundary documentation.**

Add `source_processing` to `src/osm_polygon_website_tag/application/README.md` and state that it owns one ordered source phase while `workflow` owns run lifecycle orchestration. Do not add a new dependency or command.

- [ ] **Step 5: Run the application suite and type/lint checks.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked ruff check .
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked ruff format --check .
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked ty check src tests scripts
```

Expected: all commands pass.

## Task 6: Refactor cleanup and full verification

**Files:** all changed files from Tasks 1–5

- [ ] **Step 1: Inspect the resulting module surfaces.**

Confirm `workflow.py` no longer imports pipeline, schema, or incremental-publishing details solely needed by source processing; confirm `source_processing.py` exposes only the three planned public names; confirm no `workflow` compatibility wrappers remain.

- [ ] **Step 2: Run the complete mandatory gates.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just check
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just pre-commit
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just pre-push
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just crap
```

Expected: all gates pass; CRAP reports a maximum strictly below 6.

- [ ] **Step 3: Run mutation checks for the refactored surface.**

Run the repository mutation adapter for `workflow.py` and `source_processing.py` after the final tests are copied into the mutation workspace. Confirm no `survived`, `no tests`, `timeout`, or `suspicious` result belongs to either changed module. Report unrelated pre-existing repository-wide mutation survivors separately rather than masking them.

- [ ] **Step 4: Commit the refactor.**

```bash
git add src/osm_polygon_website_tag/application/source_processing.py src/osm_polygon_website_tag/application/workflow.py src/osm_polygon_website_tag/application/README.md tests/application/test_source_processing.py tests/application/test_workflow.py
git commit -m "refactor: isolate source processing workflow"
```

## Task 7: Reintegrate onto `main` without losing user work

**Files:** no user-owned dirty files are staged by this task.

- [ ] **Step 1: Verify the refactor worktree is clean except for no intended changes.**

Run `git status --short --branch` and list the exact changed paths. The only committed changes may include the refactor and the narrowly required verification-test seam repair; do not stage `README.md`, `docs/setup.md`, `justfile`, `pyproject.toml`, `reports/`, or `slides/` from the preserved user work.

- [ ] **Step 2: Stash the preserved user work in the refactor worktree.**

Use a named `--include-untracked` stash, record its hash, and verify that the refactor branch becomes clean.

- [ ] **Step 3: Fast-forward `main` to the refactor branch and reapply the stash.**

Merge the refactor branch into `main` with the explicit branch name, then pop the named stash. If a conflict appears, preserve both the refactor and the user change; never choose a side wholesale.

- [ ] **Step 4: Run the full gates on the final `main` tree and verify the user paths remain present.**

Run `just check`, `just pre-commit`, `just pre-push`, and `just crap`; inspect `git status --short --branch` and confirm `reports/` and `slides/` remain untracked and all original user-modified paths remain modified. Do not push remotely.
