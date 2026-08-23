# Publishing

Adapts verified artifacts to Hugging Face publication.

- Modules: `hf_token`, `incremental`, `publish`, `trackio`.
- Dependencies: `reporting` and `runtime`.
- `incremental` compares content hashes for one polygon shard and the global
  README/YAML/map bundle. It uploads only changed files and atomically records
  `manifests/uploaded_polygons.json` (schema v2), which is operational state
  and is intentionally excluded from the completion receipt.
- `IncrementalPublishPlan` retains the computed shard and bundle hashes. The
  pipeline reuses that plan for the upload and checkpoint write, so each
  publication decision scans its managed artifacts once; standalone checkpoint
  calls still compute their own hashes when no plan is supplied.
- Entry points: `resolve_hf_token`, `build_publish_plan`, `publish_to_hf`, and
  `incremental_publish_changed_shard`.
- `trackio` is an optional, explicit publisher for the public dataset metrics
  dashboard. It reads only a complete run's receipt-bound Parquets and uses the
  same `CardStats` source as the generated dataset card. The normal pipeline
  does not import Trackio; install it only for the `publish-trackio --apply`
  command (for example, with `uv run --with trackio`).
- The default Space is `NoeFlandre/osm-polygon-website-tag-metrics`. Apply mode
  logs one receipt-derived run locally, then freezes it with
  `trackio.sync(..., sdk="static")` into a public, read-only Space. A stable
  receipt-derived run name and `resume="allow"` keep repeated publication of
  the same snapshot deterministic.
- Excludes: extraction, enrichment, artifact derivation, and CLI dispatch.

## Metrics contract

`build_trackio_snapshot` requires `manifests/run.json` to be in `complete`
status, a valid `manifests/completion_receipt.json`, and a fresh successful
`verify_results` check. To keep the public dashboard focused, it reports only
the headline metrics: public polygon rows, polygons with extracted text, total
extracted words, website and contact-website text coverage, source-shard count,
and occupied H3 cells. All values are recomputed from the finalized Parquets;
no hand-written counts or raw website content are sent to Trackio.

## Resumable upload checkpoint (`uploaded_polygons.json`)

Operational resume state for partial Hugging Face uploads lives in
`manifests/uploaded_polygons.json`. The shape is exposed as the
`CheckpointV2` `TypedDict` (with `schema_version: Literal["v2"]`) and
parsed/validated by small helpers in `incremental.py`
(`_parse_checkpoint`, `_validate_sources_v2`, `_validate_legacy_sources`,
`_validate_global_bundle`, `_validate_hex_sha256`). Contract:

- `schema_version` MUST be the string literal `"v2"` when the key is
  present. A **missing** `schema_version` key is the legacy case and
  migrates; a **present-but-null** value (e.g. `{"schema_version": null}`)
  is rejected with `ValueError`. Any other value is rejected.
- Malformed JSON, non-UTF-8 bytes, malformed JSON root, unsupported
  schema version, unknown `global_bundle` field, malformed
  `map_contract_version` (string, bool, non-integer), non-hex
  `<name>.osm.pbf` source key, missing/invalid `polygon_sha256`, or
  unknown per-source field all raise
  `ValueError("invalid uploaded polygon checkpoint: <reason>")` so the
  load boundary has a single, documentable failure mode.
- `global_bundle` is a `TypedDict` (`_GlobalBundleStateV2`,
  `total=False`) that permits an empty default and a partial set of the
  four known keys: `readme_sha256`, `dataset_yaml_sha256`, `map_sha256`
  (each a lowercase 64-character hex string), and
  `map_contract_version` (a non-bool integer). Any other field is
  rejected at the validation boundary.
- `sources` maps `<name>.osm.pbf` filenames to
  `{"polygon_sha256": <64-char lowercase hex>}` records. Keys must
  end with `.osm.pbf`; hashes must match the shared
  `_validate_hex_sha256` helper (`[0-9a-f]{64}`).
- Legacy checkpoints (pre-`schema_version` flat dicts that omit the
  key) are migrated silently only when every legacy entry is well-formed
  (string `.osm.pbf` key, dict value, valid 64-character hex
  `polygon_sha256`). Any malformed entry raises `ValueError`.

`load_upload_checkpoint` returns a fresh per-call dict and is safe to
mutate in place; shared mutable defaults are deliberately avoided to
keep resume state hermetic per run.

Reconciliation (`reconcile_upload_checkpoint`) validates every remote
SHA-256 via `_validate_hex_sha256` and validates the remote source
filename (must end with `.osm.pbf`) **before** rewriting
`uploaded_polygons.json`; a malformed remote hash raises
`ValueError("invalid uploaded polygon checkpoint: <reason>")` and the
existing checkpoint file remains byte-identical. The checkpoint file is
excluded from the completion receipt and from the publish plan; remote
SHA-256 hashes are authoritative during apply-mode reconciliation.
