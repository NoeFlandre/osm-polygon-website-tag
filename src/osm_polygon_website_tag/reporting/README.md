# Reporting

Builds and validates public-facing local artifacts.

- Modules: `card_stats`, `card`, `verify`, `finalize`.
- Dependencies: `contracts`, `storage`, `pipeline`, and `runtime`.
- Entry points: `compute_card_stats`, `build_card`, `verify_results`, `finalize_run`.
- Excludes: extraction, HTTP fetching, remote upload, and CLI dispatch.
