# Workflow Inventory Boundary Refactor

## Goal

Make end-to-end orchestration easier to understand by moving source inventory
and completed-bundle inspection out of `application/workflow.py`, without
changing any runtime, CLI, resume, data, or publication behavior.

## Scope

This pass may modify:

- `src/osm_polygon_website_tag/application/workflow.py`
- `src/osm_polygon_website_tag/application/inventory.py` (new)
- `src/osm_polygon_website_tag/application/README.md`
- `tests/application/test_workflow.py`
- `tests/application/test_inventory.py` (new)

No dataset schemas, manifest fields, paths, state transitions, CLI arguments,
progress messages, upload behavior, public result types, or raw-data handling
may change.

## Boundary

The new `application.inventory` module will own read-only questions about the
input inventory and existing per-source artifacts:

- recursively discover `.osm.pbf` files in deterministic relative-path order;
- reject an empty inventory;
- reject duplicate basenames with the current deterministic error message;
- compare a current fingerprint inventory with the persisted expected
  inventory;
- determine whether all three artifacts for one source match their schema,
  row-count, output-hash, and source-fingerprint contracts.

`application.workflow` will continue to own orchestration, state transitions,
extraction, enrichment, analysis, card generation, finalization, and
publication.

`workflow.discover_sources` must remain importable for compatibility. It will
be a direct imported alias of `inventory.discover_sources`, not a wrapper.
The existing private `_source_bundle_is_complete` name need not remain
available because it is not part of `workflow.__all__`; all internal callers
will import its replacement from `inventory`.

## Implementation Constraints

`discover_sources` will use `collections.Counter` to find duplicate basenames
in linear time after discovery. Its returned order and exception text will be
byte-for-byte compatible with current behavior.

Bundle verification will retain:

- exact source filename, size, and modification-time comparison;
- acceptance of public schema v1.1 or current public schema;
- exact comparison and rejection schemas;
- metadata-aware Arrow schema equality;
- exact Parquet row counts;
- exact SHA-256 output shard hashes;
- fail-closed behavior for missing or mismatched artifacts.

No generalized repository abstraction, protocol hierarchy, caching layer,
compatibility shim module, or new configuration will be introduced.

## TDD and Verification

Characterization tests will first pin deterministic recursive discovery,
duplicate-name reporting, empty and invalid roots, and each bundle-verification
failure mode. At least one deliberate temporary mutation will demonstrate that
the tests catch a contract violation; the mutation will be restored before
implementation and never staged.

After moving the code, focused inventory and workflow tests must pass, followed
by the entire repository test suite, `ty`, Ruff, wheel build, and an isolated
installed-wheel CLI smoke test.

## Acceptance Criteria

- `workflow.py` contains orchestration rather than inventory implementation.
- `inventory.py` has one documented responsibility and no write operations.
- `workflow.discover_sources` remains compatible.
- Existing error messages and progress messages are unchanged.
- Resume and artifact validation behavior is unchanged.
- Tests mirror the new module boundary and cover fail-closed cases.
- Current application documentation explains both modules accurately.
- The verified change is committed and pushed to the sole `main` branch.
