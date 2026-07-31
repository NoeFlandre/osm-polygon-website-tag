# Reporting

Builds and validates public-facing local artifacts.

- Modules: `card_stats`, `card`, `geographic`, `repair`, `verify`, `finalize`.
- Dependencies: `contracts`, `storage`, `pipeline`, and `runtime`.
- `geographic` aggregates public centroids into H3 resolution-3 counts and
  atomically renders the logarithmic `assets/geographic_polygon_density.png`
  card asset without reading PBFs or contacting the network.
- `repair.refresh_card_run` migrates a legacy completed local run by rebuilding
  only the card/map/receipt bundle; it never re-extracts or re-enriches sources.
- Entry points: `compute_card_stats`, `build_card`, `verify_results`,
  `finalize_run`, and `refresh_card_run`.
- Excludes: extraction, HTTP fetching, remote upload, and CLI dispatch.
