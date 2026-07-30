# Pipeline

Implements bounded data-processing stages.

- Modules: `extraction`, `enrich`, `analyze`, `partition_aggregate`.
- Dependencies: `contracts`, `domain`, `storage`, `web`, and `runtime`.
- Entry points: `extract_pbf`, `enrich_polygon_shard`, `analyze_results`.
- Excludes: full-run orchestration, card rendering, and remote publication.
