# Contracts

Defines the exact public, comparison, rejection, and text Arrow schemas plus
the shared dynamic compute-kernel adapter.

- Modules: `arrow`, `polygon_schema`, `language_schema`, `comparison_schema`,
  `rejection_schema`, `text_schema`.
- Dependencies: no other project package.
- Entry points: schema constants, column documentation, row validation, text statuses.
- `polygon_schema.schema_matches` is the single exact Arrow-schema comparison
  boundary; `is_supported_public_polygon_schema` covers the supported public
  migration versions (`v1.1` through `v1.4`), while v1.3 and v1.4 are current.
- `language_schema` defines the four nullable v1.4 language result fields.
- `TEXT_TERMINAL_STATUSES` (`success` and `absent`) is the shared completion
  contract used by resumable enrichment and card reporting; all other or null
  statuses remain retryable.
- `TEXT_UNFINISHED_STATUSES`, `TEXT_TRANSIENT_STATUSES`, and
  `TEXT_DETERMINISTIC_STATUSES` are the canonical resume-priority categories;
  `TEXT_NULL_STATUS` is the persisted summary sentinel for null Arrow values.
- `arrow.call_arrow_kernel` is the single dynamic dispatch boundary for
  named PyArrow compute kernels used by schema and reporting code.
- Excludes: pipeline behavior, persistence, and remote adapters.
