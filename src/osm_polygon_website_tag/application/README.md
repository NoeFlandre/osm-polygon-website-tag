# Application

Composes the complete application.

- Modules: `workflow`, `cli`.
- Dependencies: any lower project package; no lower package may import `application`.
- Entry points: `run_all`, `discover_sources`, and CLI `main`.
- Excludes: reusable domain rules, storage primitives, and stage implementations.
