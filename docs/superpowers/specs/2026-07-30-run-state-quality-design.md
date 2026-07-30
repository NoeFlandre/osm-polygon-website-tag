# Run-State Quality Pass

## Goal

Make the run-state implementation and its tests easier to understand and
maintain without changing any observable pipeline behavior.

## Scope

This pass is limited to:

- `src/osm_polygon_website_tag/runtime/run_state.py`
- `tests/runtime/test_run_state.py`
- directly relevant runtime documentation, only if the refactor makes it stale

It will not change CLI arguments, public function signatures, run-directory
layout, manifest filenames, JSON fields, JSON ordering, status transitions,
source identity rules, dataset schemas, extraction, enrichment, publication,
or resume behavior.

## Design

The production module will gain only small private helpers where they remove
real duplication, principally for serializing the sorted source manifest and
the expected-source inventory. Public functions will remain the integration
boundary and retain their current names, parameters, return values, exceptions,
and side effects.

The test module will use normal module-level imports instead of repeated local
imports and dynamic `__import__` calls. Stale type suppressions will be removed.
Test helpers will remain local and will describe behavior rather than mirror
production implementation.

No compatibility facade, abstraction layer, generalized manifest framework, or
new configuration will be introduced.

## TDD and Verification

Before production edits, characterization tests will pin:

- the exact expected-source inventory structure and deterministic order;
- the exact processed-source manifest structure and deterministic order;
- status persistence across a legal transition;
- rejection of implicit status mutation.

Each new or strengthened test must first be demonstrated to fail for the
intended reason when it represents a missing seam or invariant. Production
changes will then be the minimum required to make the test pass. Pure test
cleanup that removes stale suppressions is verified with `ty`.

Completion requires:

1. focused runtime tests;
2. the full test suite;
3. `uv run ty check src tests`;
4. Ruff lint and format checks;
5. a wheel build and installed CLI smoke test;
6. a clean, reviewed diff containing no unrelated changes.

## Acceptance Criteria

- Runtime and dataset behavior are unchanged.
- Public run-state API signatures are unchanged.
- All stale type suppressions and dynamic imports in
  `tests/runtime/test_run_state.py` are gone.
- Repeated manifest-writing logic is reduced only where a private helper makes
  ownership clearer.
- Current documentation remains accurate.
- All quality gates pass before the change is committed and pushed to the sole
  `main` branch.
