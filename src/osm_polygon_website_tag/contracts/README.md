# Contracts

Defines the exact public, comparison, rejection, and text Arrow schemas.

- Modules: `polygon_schema`, `comparison_schema`, `rejection_schema`, `text_schema`.
- Dependencies: no other project package.
- Entry points: schema constants, column documentation, row validation, text statuses.
- `TEXT_TERMINAL_STATUSES` (`success` and `absent`) is the shared completion
  contract used by resumable enrichment and card reporting; all other or null
  statuses remain retryable.
- Excludes: pipeline behavior, persistence, and remote adapters.
