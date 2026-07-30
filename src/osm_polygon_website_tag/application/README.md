# Application

Composes the complete application while keeping read-only inventory inspection
separate from workflow side effects.

- Modules:
  - `inventory`: discovers source PBFs and verifies persisted source and shard
    inventories. It performs no writes.
  - `workflow`: owns resumable orchestration, state transitions, and calls into
    extraction, enrichment, reporting, and publication.
  - `cli`: parses commands and delegates to application entry points.
- Dependencies: any lower project package; no lower package may import `application`.
- Entry points: `inventory.discover_sources`, `workflow.run_all`, the
  compatibility import `workflow.discover_sources`, and CLI `main`.
- Excludes: reusable domain rules, storage primitives, stage implementations,
  and inventory writes.
