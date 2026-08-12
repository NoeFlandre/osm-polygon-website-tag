# Application

Composes the complete application while keeping read-only inventory inspection
separate from workflow side effects.

- Modules:
  - `inventory`: discovers source PBFs and verifies persisted source and shard
    inventories. It performs no writes.
  - `workflow`: owns resumable per-source extraction, enrichment, upload
    transactions, run-level state transitions, reporting, and final publication.
  - `resume_planner`: owns deterministic source ordering, bounded text-status
    summaries, and durable partial-checkpoint discovery. It has no network or
    PBF side effects; `workflow` only composes its plan with the stage calls.
  - `progress`: adapts workflow messages to stable logs or interactive tqdm
    progress without leaking terminal concerns into the pipeline.
  - `cli`: exposes the typed Typer application, uses Rich for human-facing
    stderr, and delegates to application entry points.
- Dependencies: any lower project package; no lower package may import `application`.
- Entry points: `inventory.discover_sources`, `workflow.run_all`, the
  compatibility import `workflow.discover_sources`, Typer `app`, and CLI
  compatibility function `main`.
- Excludes: reusable domain rules, storage primitives, stage implementations,
  and inventory writes.
