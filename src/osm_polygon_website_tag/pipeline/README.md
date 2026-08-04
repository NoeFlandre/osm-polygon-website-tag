# Pipeline

Implements bounded data-processing stages.

- Modules: `extraction`, `record_builders`, `enrich`, `public_schema_migration`, `analyze`,
  `partition_aggregate`.
- Dependencies: `contracts`, `domain`, `storage`, `web`, and `runtime`.
- Entry points: `extract_pbf`, `enrich_polygon_shard`,
  `migrate_public_shard`, `analyze_results`.
- Excludes: full-run orchestration, card rendering, and remote publication.

## Record builders

`record_builders` is a pure `domain`-only helper that factors out the
tag-derived values reused by the extraction row builders.

`derive_tags` is the projection shared by all three row builders
(`_public_record`, `_comparison_record`, `_rejection_record`). It returns
exactly:

- normalized `website` and `contact:website`,
- the website presence flags (`has_website`, `has_contact_website`,
  `has_any_website`),
- the primary category.

It is computed once per builder invocation and never includes URL
classification or hostname extraction -- those are public-row-only and
remain in `extraction._public_record`.

`derive_wikidata` is a separate small helper that returns the normalized
`wikidata` value and its presence flag. It is used only by
`_comparison_record` and `_rejection_record`; the public shard's v1.3
schema omits Wikidata, so `_public_record` does not call it.
