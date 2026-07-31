# Publishing

Adapts verified artifacts to Hugging Face publication.

- Modules: `hf_token`, `incremental`, `publish`.
- Dependencies: `reporting` and `runtime`.
- `incremental` compares content hashes for one polygon shard and the global
  README/YAML/map bundle. It uploads only changed files and atomically records
  `manifests/uploaded_polygons.json` (schema v2), which is operational state
  and is intentionally excluded from the completion receipt.
- Entry points: `resolve_hf_token`, `build_publish_plan`, `publish_to_hf`, and
  `incremental_publish_changed_shard`.
- Excludes: extraction, enrichment, artifact derivation, and CLI dispatch.
