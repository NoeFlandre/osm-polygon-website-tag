# Storage

Provides bounded and transactional local persistence.

- Modules: `atomic`, `batch_sink`, `candidate_ledger`, `duckdb_engine`.
- Dependencies: no other project package.
- Entry points: atomic promotion, bounded Parquet writes, SQLite ledgers, DuckDB setup.
- Excludes: run lifecycle, pipeline sequencing, and remote publication.

`atomic_promote_bundle` moves existing targets to tokenized backups before
promoting staged files, then removes backups only after every forward rename
succeeds. Its optional test mover observes both backup and promotion renames;
rollback deliberately uses the default atomic rename so an injected failure
cannot disable restoration.

## Candidate ledger

`candidate_ledger` is per-PBF extraction scratch state, not a resume checkpoint.
It batches SQLite mutations behind a bounded commit interval
(`DEFAULT_COMMIT_BATCH_SIZE`, default 4096) to amortize journal `fsync` cost
during extraction, flushes any pending mutations on `close()`, and is deleted
after successful extraction. Reads (`get`, `missing_areas`) run on the same
connection and see uncommitted rows, so reconciliation semantics are unchanged.
The public, comparison, and rejection Parquet outputs are promoted atomically
only after extraction succeeds.
