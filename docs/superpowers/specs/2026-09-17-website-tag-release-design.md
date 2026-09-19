# Website-tag deterministic release design

## Goal

Finish the Website-tag release without rebuilding the validated dataset: every
card, map, report, receipt, and remote verification result must use explicit,
deterministic populations and must agree on the same source snapshot.

## Scope

This change is limited to `NoeFlandre/osm-polygon-website-tag` and the
`NoeFlandre/osm-polygon-website-tag` Hugging Face dataset. It covers issues
#3, #6, #7, #8, and #9. It does not modify the Description-tag, Wikidata,
benchmark, FinePDF, or unrelated repositories.

The existing validated public Parquet data is reused. Raw PBF processing is
not repeated unless independent verification proves that no complete run is
available. Existing untracked `reports/` and `slides/` remain untouched.

## Population contract

The canonical text population is one identity per `(osm_type, osm_id)` across
all regional public shards. A row qualifies only when its field status is the
documented success value and its extracted text is non-null, trimmed, and
non-empty. `website` and `contact:website` retain separate counts; the
combined count is the union of their identities.

For each qualifying identity, the reducer selects one canonical coordinate and
payload using the repository's stable deterministic ranking, with all ties
resolved by stable source and row keys. Input path order and traversal order
cannot change the result. The reducer is shared by card statistics, word and
language totals that claim polygon counts, geographic aggregation, captions,
machine-readable release statistics, and completion evidence.

Regional observations, published rows, duplicate observations, unique
identities, per-tag successful-text identities, combined successful-text
identities, rejected candidates, area-statistics rows, and word-count rows are
separate named populations. Existing regional-row metrics remain available but
are labeled as regional observations.

The geographic map represents exactly one canonical centroid per combined
successful-text identity. Its caption explicitly says that regional overlap
duplicates were removed globally. The map point count, combined card count,
`stats.json` population field, release evidence, and remote verification must
agree.

Geometry statistics remain computed over canonical published polygon rows,
not the text-qualified map population. The machine-readable report and card
state this population explicitly and use deterministic sorted input, stable
rounding, and the existing bounded/spillable computation.

## Artifact and card design

The existing release machinery is extended rather than replaced. README
front matter, provenance, source descriptions, language and sentence sections,
schema, methodology, licensing, links, limitations, hostname tables, and
Trackio links are preserved byte-for-byte except for their generated statistic,
map-caption, geometry, receipt, or related wording blocks.

`README.md`, `dataset.yaml`, the geographic PNG, and `stats.json` are promoted
as one local artifact bundle. A fresh release refreshes all derived geography
and geometry artifacts from one summary. A golden preservation test compares
unrelated card sections before and after regeneration.

## Verification and release design

The local verifier accepts legacy completion receipts without
`data_manifest_sha256`, while new receipts bind the source-manifest and
Parquet identity. It independently checks local Parquet and manifest
inventories, and the release verifier independently checks remote Parquet
bytes, remotely published manifests, README/statistics/map bytes, the remote
revision, schema, representative rows, and Dataset Viewer validity.

Remote receipts are never treated as the only proof of data identity. A
changed Parquet shard or manifest fails verification even when the receipt is
unchanged. If documented migration behavior permits it, missing legacy receipt
fields are backfilled only when the card bytes are unchanged. Repeating the
same release after a successful publication must report and independently
prove an exact no-op.

The release plan is inspected before the one authorized `release-stats
--apply`; it may contain only intended metadata, report, and map changes. The
validated run is selected by complete manifests, source inventory, Parquet
inventory, status, receipt, and card verification. A run marked incomplete is
not published merely because its counts resemble the remote card.

## Mutation and quality design

The mutation source mapper treats `reporting/geographic/__init__.py` as the
package module itself, keeps root-package and ordinary nested-module mappings
correct, and proves the old failure with a RED regression test. The mutation
runner rejects zero generated mutants and mutants with no associated tests;
all twelve previously failing shards must produce meaningful results without
threshold weakening, changed-source exclusions, or baseline hiding.

The implementation follows RED -> GREEN -> REFACTOR for each contract. Tests
cover duplicate regional rows and overlapping shards, conflicting geometry and
text values, status and whitespace rules, both website fields, order
independence, deterministic map/statistics output, captions, area populations,
receipt compatibility, remote data/manifest mismatches, stale or missing
`stats.json`, README preservation, mutation scope, zero-mutant/no-test guards,
and repeated release no-ops.

## Delivery sequence

1. Implement and locally verify the shared population contract, card/map/
   geometry artifacts, release verification, and mutation fixes.
2. Run the required local quality gauntlet and record exact results.
3. Obtain independent AI review, address findings, and merge the accepted PR.
4. Select and verify the complete HDD run, inspect the dry-run plan, apply the
   single authorized Hugging Face release, and independently verify every
   remote artifact.
5. Run the same release again and prove the exact no-op.
6. Update issues #3, #6, #7, #8, and #9 plus the GeoReSeT project only as
   supported by evidence; mark work Done only after merge, acceptance, and
   verified publication.
