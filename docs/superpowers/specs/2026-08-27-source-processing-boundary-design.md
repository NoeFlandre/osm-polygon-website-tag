# Source Processing Boundary Design

## Goal

Reduce the cohesion burden of `application/workflow.py` by moving the complete
per-source transaction behind one deep, typed application boundary. Preserve the
public `run_all()` API, run-state transitions, artifact formats, checkpoint
semantics, upload behavior, and all default/opt-in behavior.

## Current problem

`application/workflow.py` is approximately 1,200 lines and combines two
different responsibilities:

1. Run orchestration: initialization, snapshot reopening, source ordering,
   phase transitions, aggregate analysis, card generation, finalization, and
   complete-run publication.
2. Source processing: source-bundle extraction, public-schema migration, text
   enrichment, optional GlotLID detection, incremental shard publication, and
   per-source checkpoint updates.

The source-processing responsibility is already a coherent transaction, but its
implementation is interleaved with the run-level state machine. This makes the
orchestrator harder to read and forces tests to reach many private helpers.
Current quality gates are strong enough to support a safe refactor, but the
merged `main` baseline must be green before source movement begins.

## Chosen architecture

Create `application/source_processing.py` as a deep module with this small
public surface:

- `SourceProcessingContext`: immutable configuration and mutable run-state
  references required by one invocation.
- `SourcePhaseCounts`: extracted, reused, and uploaded counts returned to the
  run orchestrator.
- `process_sources(...)`: processes the ordered source set for either the
  extraction or enrichment phase.

The module owns all per-source details: bundle readiness and extraction,
schema migration, text enrichment, optional language detection, incremental
publication, upload-checkpoint updates, shard-path calculation, and shard
enrichment/status inspection. Its private helpers remain implementation details.

`application/workflow.py` keeps the run-level responsibilities: loading or
initializing state, freezing/reopening snapshots, source prioritization, phase
transitions, aggregate analysis, card generation, final verification, and
complete-run publication. `run_all()` continues to construct the context and
delegate source phases; its signature and returned `WorkflowResult` remain
unchanged.

No new dependency, schema, command, environment variable, data path, or
network behavior is introduced. Existing production paths remain governed by
the Seagate boundary.

## Data flow and failure behavior

For each phase, `workflow` supplies the discovered sources, deterministic order,
source fingerprints, and one `SourceProcessingContext` to `process_sources()`.
The deep module processes one source at a time and returns aggregate counts.
The existing atomic shard promotion, enrichment checkpoints, language
checkpoints, upload checkpoint, and uncaught `KeyboardInterrupt` behavior are
retained exactly. A failure leaves the same durable checkpoints and does not
advance the run state beyond the existing transition points.

The refactor will not add compatibility wrappers for private workflow helpers.
Tests that currently exercise those helpers will move to the new module's
boundary or become characterization tests of `process_sources()`. Public
imports and CLI behavior will remain compatible.

## Test and quality strategy

1. Repair the merged-main verification test's monkeypatch seam so the baseline
   exercises the current public-schema helper and passes without changing
   production behavior.
2. Add a failing source-processing boundary test that expresses the existing
   source transaction result and phase counts.
3. Implement the smallest context/result/facade needed to make that test pass.
4. Move the existing source-processing implementation and update workflow
   callers and tests while keeping the full suite green.
5. Run targeted tests after each extraction step, then the complete `just check`,
   `just pre-commit`, and `just pre-push` gates.
6. Run `just crap` and ensure the maximum CRAP score remains strictly below 6.
   Run mutation testing for the changed workflow/source-processing modules;
   report any unrelated repository-wide baseline survivors separately.

Success means identical observable behavior, a materially smaller and simpler
`workflow.py`, a focused source-processing module with a narrow public surface,
all tests passing, and no new mutation or CRAP regressions.
