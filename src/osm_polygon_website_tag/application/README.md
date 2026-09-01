# Application

Composes the complete application while keeping read-only inventory inspection
separate from workflow side effects.

- Modules:
  - `inventory`: discovers source PBFs and verifies persisted source and shard
    inventories. It performs no writes.
  - `source_processing`: owns one ordered source phase, including resumable
    extraction, enrichment, language detection, incremental publication, and
    source-level metadata/checkpoint updates.
  - `workflow`: owns run-level lifecycle orchestration, status transitions,
    reporting, and final publication; it delegates source work to
    `source_processing`.
  - `resume_planner`: owns deterministic source ordering, bounded text-status
    summaries, and durable partial-checkpoint discovery. It has no network or
    PBF side effects; `workflow` only composes its plan with the stage calls.
  - `progress`: adapts workflow messages to stable logs or interactive tqdm
    progress without leaking terminal concerns into the pipeline.
  - `cli`: exposes the typed Typer application, uses Rich for human-facing
    stderr, and delegates to application entry points.
  - `grid5000_runner`: provides the dependency-light, offline entry point for
    one staged language-detection bundle on a reserved compute node.
- Dependencies: any lower project package; no lower package may import `application`.
- Entry points: `inventory.discover_sources`, `workflow.run_all`, the
  compatibility import `workflow.discover_sources`, Typer `app`, CLI
  compatibility function `main`, and `grid5000_runner.main`.
- Excludes: reusable domain rules, storage primitives, stage implementations,
  and inventory writes.
