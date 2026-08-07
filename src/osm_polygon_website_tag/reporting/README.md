# Reporting

Builds and validates public-facing local artifacts.

- Modules: `card_stats`, `card`, `geographic`, `repair`, `verify`, `finalize`.
- Dependencies: `contracts`, `storage`, `pipeline`, and `runtime`.
- `geographic` aggregates public centroids into H3 resolution-3 counts and
  atomically renders the logarithmic `assets/geographic_polygon_density.png`
  card asset without reading PBFs or contacting the network. The dataset-card
  map explicitly counts only polygons with a successful website or
  `contact:website` text extraction.
- `repair.refresh_card_run` migrates a legacy completed local run by rebuilding
  only the card/map/receipt bundle; it never re-extracts or re-enriches sources.
- Card text totals scan Parquet columns with bounded Arrow kernels rather than
  materializing one Python row dictionary per polygon.
- Entry points: `compute_card_stats`, `build_card`, `verify_results`,
  `finalize_run`, and `refresh_card_run`.
- Excludes: extraction, HTTP fetching, remote upload, and CLI dispatch.
