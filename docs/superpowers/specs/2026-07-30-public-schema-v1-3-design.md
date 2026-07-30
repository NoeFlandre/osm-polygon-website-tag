# Public Polygon Schema v1.3 Migration

## Goal

Remove six redundant public polygon columns:

- `preferred_website`
- `preferred_website_source`
- `wikidata`
- `wikidata_qid`
- `wikidata_class`
- `area_km2`

The comparison/analysis artifacts retain their Wikidata fields so the published
overlap analysis and automatically computed dataset-card statistics remain
factual. The raw `tags` payload also remains unchanged.

## Public contract

`POLYGON_PUBLIC_SCHEMA` becomes v1.3. New extraction writes v1.3 directly.
Existing v1.2 polygon Parquets are migrated by selecting the retained columns,
setting every row's `schema_version` to `v1.3`, preserving field metadata,
and atomically replacing the source shard. Row order, row count, geometry,
website text, text status, and word counts must remain unchanged.

The migration never opens a PBF and never fetches a website. v1.1 shards still
follow the existing text-enrichment path and are emitted directly as v1.3.

## Resume and publication

On `run-all` resume, each verified completed source bundle is handled as follows:

1. Detect its public schema.
2. If v1.2, perform the bounded transactional schema-only migration.
3. Update the run manifest with the new shard hash and unchanged row count.
4. Recompute the cumulative card from current Parquets.
5. Let the existing content-hash upload checkpoint detect the changed shard
   and upload it before advancing to the next PBF.

Already-v1.3 shards whose upload checkpoint matches are skipped. Interrupted
migrations leave the original shard intact; interrupted uploads resume through
the existing acknowledgement file. No blanket checkpoint deletion is needed.

## Safety

- Raw PBFs remain read-only and unopened during migration.
- Migration is bounded by the existing Parquet batch size.
- Source-bundle verification remains fail-closed.
- Unknown public schemas fail with a clear error.
- The currently running old-code process must exit before the migrated command
  is started; two processes must never mutate the same run directory.

## Testing

RED-to-GREEN tests cover:

- the exact v1.3 public column list and metadata;
- direct v1.3 extraction;
- v1.2-to-v1.3 migration preserving retained values and text;
- legacy v1.1 enrichment directly to v1.3;
- resume reusing completed PBF extraction without reopening PBFs or refetching
  successful website text;
- changed shard hashes causing reupload while matching v1.3 checkpoints skip;
- interruption safety and full synthetic acceptance;
- card documentation excluding removed public columns while analysis remains
  based on comparison artifacts.

Ruff, Ruff formatting, ty, pytest, pre-commit, build, wheel smoke, and GitHub
Actions must pass before the user is told it is safe to resume.
