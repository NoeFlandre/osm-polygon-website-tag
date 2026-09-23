# Reporting

Builds and validates public-facing local artifacts.

- Modules: `artifact_inventory`, `card`, `card_rendering`, `card_metadata`,
  `card_patching`, `card_stats`, `geometry_stats`, `geographic`, `repair`,
  `verify`, and `finalize`.
- Dependencies: `contracts`, `storage`, `pipeline`, and `runtime`.
- `geographic` aggregates public centroids into H3 resolution-3 counts and
  atomically renders the logarithmic `assets/geographic_polygon_density.png`
  card asset without reading PBFs or contacting the network. The
  `global_unique_text` mode reduces regional overlap by `(osm_type, osm_id)`
  with deterministic canonical winners; the dataset-card map uses that mode
  and explicitly counts only polygons with successful, non-empty website or
  `contact:website` text.
- `repair.refresh_card_run` migrates a legacy completed local run by rebuilding
  only the card/map/receipt bundle; it never re-extracts or re-enriches sources.
- `finalize_snapshot` finishes an explicitly frozen (`snapshot_status: done`)
  run from its existing shards without enrichment. It rejects unfinished
  `pending` text rows, preserves recorded retry/failure outcomes, and builds
  the analysis/card/map/receipt bundle.
- Card text totals scan Parquet columns with bounded Arrow kernels rather than
  materializing one Python row dictionary per polygon.
- Global card text populations use the same spillable DuckDB reducer as the
  global geographic map, so tag-specific counts and combined identity counts
  share one deterministic definition.
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
- `card` is the compatibility-preserving orchestration entry point for fresh
  card builds and legacy geometry updates. `card_rendering` owns pure Markdown
  sections, `card_metadata` owns YAML/front-matter contracts, and
  `card_patching` owns byte-preserving legacy-card updates (public
  `update_*_section` functions). `card` no longer re-exports helper names;
  import them from their owning module. The patcher refreshes only website
  text, languages, sentences, geography, and geometry, inserting missing ones
  in `render_markdown` order. The snapshot ("At a glance"), hostname, and
  method sections are frozen on legacy cards and keep their original counts;
  regenerate the full card through a release when they must change.
  Release-time map, README, YAML, and
  geometry promotion lives under `publishing/card_artifacts`.
- `verify` is the stable verification entry point; its internal section
  validators live under `verification/` and are not public API. They cover
  rows, shards, text, language pairs, sentence segmentation, analysis and card
  output, and the completion receipt.
- `geometry_stats` computes the deterministic polygon surface, shape, and
  extent statistics of every validated public row, streaming geometry decoding
  one record batch at a time. It also embeds the canonical global text
  population used by the card and map. `build_card` writes the result to
  `stats.json` and renders the card's geometry block from the same geometry
  result; the file is only rewritten when its bytes change, and verification
  rejects a missing or stale one.
- Entry points: `compute_card_stats`, `compute_geometry_stats`, `build_card`,
  `verify_results`, `finalize_run`, `finalize_snapshot`, and `refresh_card_run`.
- Excludes: extraction, HTTP fetching, remote upload, and CLI dispatch.
