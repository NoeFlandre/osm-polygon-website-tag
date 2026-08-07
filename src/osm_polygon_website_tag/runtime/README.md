# Runtime

Defines configuration, local paths, safety, and resumable run lifecycle.

- Modules: `config`, `paths`, `safety`, `run_state`.
- Dependencies: no other project package.
- Entry points: settings, safe path resolution, run initialization and transitions.
- Excludes: extraction, reporting, publication, and application orchestration.
- Resume manifests are UTF-8 JSON and are validated at the load boundary: source
  entries must be objects with unique filenames and non-boolean integer size/mtime
  fingerprints. Corrupt or ambiguous state fails closed with `ValueError`.
