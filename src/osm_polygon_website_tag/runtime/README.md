# Runtime

Defines configuration, local paths, safety, and resumable run lifecycle.

- Modules: `clock`, `config`, `paths`, `safety`, `run_state`.
- Dependencies: `storage` (shard hashing only).
- Entry points: `Settings()` configuration, safe path resolution, run initialization and transitions,
  and the UTC clock helpers in `clock` (`utc_now`, `utc_iso`, `utc_iso_seconds`, `utc_run_id`). Each
  helper keeps the exact string format already stored in run and extraction records.
- Excludes: extraction, reporting, publication, and application orchestration.
- Resume manifests are UTF-8 JSON and are validated at the load boundary: source
  entries must be objects with unique filenames and non-boolean integer size/mtime
  fingerprints. Corrupt or ambiguous state fails closed with `ValueError`.
