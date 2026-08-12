# Contracts

Defines the exact public, comparison, rejection, and text Arrow schemas.

- Modules: `polygon_schema`, `comparison_schema`, `rejection_schema`, `text_schema`.
- Dependencies: no other project package.
- Entry points: schema constants, column documentation, row validation, text statuses.
- `polygon_schema.schema_matches` is the single exact Arrow-schema comparison
  boundary; `is_supported_public_polygon_schema` covers the supported public
  migration versions (`v1.1`, `v1.2`, and `v1.3`).
- `TEXT_TERMINAL_STATUSES` (`success` and `absent`) is the shared completion
  contract used by resumable enrichment and card reporting; all other or null
  statuses remain retryable.
- Excludes: pipeline behavior, persistence, and remote adapters.
