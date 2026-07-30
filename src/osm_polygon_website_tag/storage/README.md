# Storage

Provides bounded and transactional local persistence.

- Modules: `atomic`, `batch_sink`, `candidate_ledger`, `duckdb_engine`.
- Dependencies: no other project package.
- Entry points: atomic promotion, bounded Parquet writes, SQLite ledgers, DuckDB setup.
- Excludes: run lifecycle, pipeline sequencing, and remote publication.
