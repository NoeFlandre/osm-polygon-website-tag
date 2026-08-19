# Reporting

Builds and validates public-facing local artifacts.

- Modules: `artifact_inventory`, `card_stats`, `card`, `geographic`, `repair`,
  `verify`, `finalize`.
- Dependencies: `contracts`, `storage`, `pipeline`, and `runtime`.
- `geographic` aggregates public centroids into H3 resolution-3 counts and
  atomically renders the logarithmic `assets/geographic_polygon_density.png`
  card asset without reading PBFs or contacting the network. The dataset-card
  map explicitly counts only polygons with a successful website or
  `contact:website` text extraction.
- `repair.refresh_card_run` migrates a legacy completed local run by rebuilding
  only the card/map/receipt bundle; it never re-extracts or re-enriches sources.
- `finalize_snapshot` finishes an explicitly frozen (`snapshot_status: done`)
  run from its existing shards without enrichment. It rejects unfinished
  `pending` text rows, preserves recorded retry/failure outcomes, and builds
  the analysis/card/map/receipt bundle.
- Card text totals scan Parquet columns with bounded Arrow kernels rather than
  materializing one Python row dictionary per polygon.
- A source contributes to the card's enriched count only when every website and
  contact-website text status is terminal (`success` or `absent`), matching the
  workflow's resumable retry contract.
- A user-frozen run may set `manifests/run.json` `snapshot_status` to `done`;
  the card then labels that published snapshot `Done` while retaining all
  retry and failure counts. Once its completion receipt exists, resuming
  `run-all` is a no-op and does not retry or upload anything.
- `artifact_inventory` is the shared source of truth for deterministic
  publishable paths and bounded SHA-256 hashing used by both finalization and
  receipt verification.
- `verify` is the stable verification entry point; its internal section
  validators live under `verification/` and are not public API.
- Entry points: `compute_card_stats`, `build_card`, `verify_results`,
  `finalize_run`, `finalize_snapshot`, and `refresh_card_run`.
- Excludes: extraction, HTTP fetching, remote upload, and CLI dispatch.
