# Contracts

Defines the exact public, comparison, rejection, and text Arrow schemas plus
the shared dynamic compute-kernel adapter.

- Modules: `arrow`, `polygon_schema`, `comparison_schema`, `rejection_schema`,
  `text_schema`.
- Dependencies: no other project package.
- Entry points: schema constants, column documentation, row validation, text statuses.
- `polygon_schema.schema_matches` is the single exact Arrow-schema comparison
  boundary; `is_supported_public_polygon_schema` covers the supported public
  migration versions (`v1.1`, `v1.2`, and `v1.3`).
- `TEXT_TERMINAL_STATUSES` (`success` and `absent`) is the shared completion
  contract used by resumable enrichment and card reporting; all other or null
  statuses remain retryable.
- `TEXT_UNFINISHED_STATUSES`, `TEXT_TRANSIENT_STATUSES`, and
  `TEXT_DETERMINISTIC_STATUSES` are the canonical resume-priority categories;
  `TEXT_NULL_STATUS` is the persisted summary sentinel for null Arrow values.
- `arrow.call_arrow_kernel` is the single dynamic dispatch boundary for
  named PyArrow compute kernels used by schema and reporting code.
- Excludes: pipeline behavior, persistence, and remote adapters.
