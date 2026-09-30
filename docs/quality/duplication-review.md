# Duplication review

Reviewed on 2026-09-30 for production logic and test setup in this repository.

## Scan method and limits

A one-off Python AST scan covered `src/osm_polygon_website_tag/**/*.py`,
`scripts/**/*.py`, and `tests/**/*.py`. It removed function docstrings and
source locations, consistently renamed local identifiers, parameters, and
nested definition names within each body, then grouped identical normalized
trees containing at least 24 AST nodes. It counted 1,370 production and 2,881
test function bodies; 1,076 and 2,079, respectively, met the size cutoff.
Among those, it found three repeated production-body groups (seven bodies)
and 13 repeated test-body groups (26 bodies). These counts describe exact
structural matches above that size cutoff; they do not establish that the
repository has no duplication. The scan does not detect fuzzy similarity,
repeated module-level setup, or smaller helpers. Each match was reviewed in
context before changing code.

## Consolidated in this change

- `pipeline.time_budget` now owns the deadline, remaining-time, exhaustion,
  and batch-option rules shared by language detection and sentence runs.
- `reporting.artifact_inventory.source_scoped_parquet_paths` owns sorted,
  source-filtered Parquet selection used by card statistics, geometry
  statistics, and geographic inputs.
- `reporting.geometry_stats.stage_geometry_stats` owns the canonical geometry
  statistics render-and-stage operation used by both card building and
  publication.
- `reporting.verify` shares status-based analysis/card and receipt checks;
  strict and release-compatible card verification remain explicit policies.
- `application.source_processing._run_has_shard_needing` now owns sorted shard
  traversal while enrichment and language detection keep separate predicates.
- `scripts.quality.mutation_gate._mutants_matching` shares the matched-name
  collection used for killed and unchecked verdicts.
- Workflow tests share offline remote and static-enrichment fixtures, common
  synthetic sources and detector helpers. Extraction tests share their XML,
  PBF-builder, and website-payload setup. Publishing tests now share finalized
  run setup and completion-receipt instrumentation.
- Workflow recovery tests share extraction tracing. CRAP-report tests now use
  one path-parameterized subprocess helper. A duplicated paused-receipt test
  and duplicate empty-shard adapter test were removed after verifying that
  their bodies and inputs were identical to retained coverage.

## Intentional local matches

The three remaining repeated production groups are small and policy-specific:

- The two Grid'5000 sync commands expose distinct language and sentence
  operations and payloads through Typer. Keeping each command explicit keeps
  its options and help text attached to the correct operation.
- Three schema modules expose the same tiny `column_doc` accessor, but each
  looks up its own versioned public contract and owns its schema error context.
- The deduplication and text-population SQL modules each extract one scalar
  result through their local connection API. Their overlapping two-line
  helpers do not justify a shared database abstraction.

The remaining test matches are scenario-specific cases and setup around
different contracts: schema-version writers, distinct detector outputs,
retry/interruption paths, independent remote-client responses, release and
remote-identity failures, and alternative terminal-state transitions. Shard
writers in the language and text-verification tests use different Arrow
schemas; detector fakes also return different predictions. The remaining
matches keep each fixture or assertion tied to the schema or behavior it
protects. Larger copied workflow, extraction, and receipt setup was moved to
shared helpers, and exact duplicate tests were removed.

## Cross-repository candidates and ownership

A read-only production scan across this repository and `osm-worldcover` found
no exact alpha-normalized production function-body clones. Both repositories
use a CRAP formula and a strict `<6` limit, but their adapters and coverage
semantics differ: this repository uses coverage.py per-function coverage and
fails on missing function entries; WorldCover derives executed and missing
lines through its Radon subprocess adapter. Keep the adapters repository-owned
until Noé and maintainers of both repositories agree on canonical coverage
semantics, dependency ownership, tests, and a named maintainer for any shared
implementation. No shared package is proposed in this review.

Mutation thresholds are also intentionally repository-specific. WorldCover's
current score gate treats timeouts as killed and uses an 80% floor, while this
repository uses a strict baseline ratchet and treats timeouts and other
unverified outcomes as failures. Do not copy the WorldCover threshold into
this repository. The overall and per-area thresholds and their denominator for
issue #91 still need the repository owner's decision.

Manifest generation and stable artifact writing look similar across the two
repositories, but their schemas and atomic-write contracts differ. No shared
runtime helper is justified without an owner-approved contract. This review
did not modify `osm-worldcover` or the excluded geoparser repository.
