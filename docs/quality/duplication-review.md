# Duplication review

Reviewed on 2026-09-30 for production logic and test setup in this repository.

## Scan method and limits

A one-off Python AST scan covered `src/osm_polygon_website_tag/**/*.py`,
`scripts/**/*.py`, and `tests/**/*.py`. It removed function docstrings and
source locations, consistently renamed identifiers, parameters, nested
definition names, and keyword labels within each body, then grouped identical
normalized trees containing at least 24 AST nodes. It counted 1,370
production and 2,961 test function bodies; 1,076 and 2,132, respectively, met
the size cutoff. Among those, it found three repeated production-body groups
(seven bodies) and five repeated test-body groups (10 bodies). Nested and
asynchronous function bodies are counted separately. This scan normalizes
local identifiers, parameters, nested definition names, and keyword labels; it
preserves attribute names and schema constants. These counts
describe exact structural matches above that size cutoff; they do not
establish that the repository has no duplication. The scan does not detect
fuzzy similarity, repeated module-level setup, or smaller helpers. Each match
was reviewed in context before changing code.

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
- Workflow recovery tests share extraction tracing. Remote identity tests use
  a shared read-only Hub API double. CRAP-report tests now use
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

The five remaining test matches each preserve a separate contract: workflow and
language-detection fakes return different predictions; polygon migration
writers target v1.1 and v1.2 schemas; enrichment tests record the first fetch
and resumed fetch independently; checkpoint tests assert different polygon
and sentence schemas; and language versus text-verification shard writers use
different Arrow schemas. These helpers are small schema- or scenario-specific
fixtures, so extracting them would add indirection without consolidating
substantial behavior. Larger copied workflow, extraction, remote-client, and
receipt setup was moved to shared helpers, and exact duplicate tests were
removed.

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

Issue #82 proposed sharing Website's fetcher and `storage/atomic.py` with
`osm-polygon-description-tag` and `osm-polygon-wikidata-only`. A read-only
source audit inspected Description at `61a053d`, Wikidata at `fd3ff315`, and
Website at `8e36215`. The Website audit pin predates the current PR98 head
`4c0b1c2e9bc5fc352aa7e69560baf66a78b9c304`. Direct blob comparisons at
`8e36215` and earlier PR98 head `004e7e8f85c83120d4a5dc02b8f2c30f0cae1460`
confirmed byte-identical audited files: `web/web_fetch.py` (`b599af46`),
`storage/atomic.py` (`07eb805b`), `runtime/run_state.py` (`d01b0a96`), and
`tests/web/test_web_fetch.py` (`7e79d67f`). A GitHub compare from `004e7e8`
through current head `4c0b1c2` lists only five changed files:
`docs/quality/duplication-review.md`, `docs/quality/mutation-baseline.txt`,
`tests/application/test_cli_adapter_contracts.py`, `tests/pipeline/test_enrich.py`,
and `tests/web/test_politeness.py`. None of the four audited files changed,
so the source-audit findings carry forward through current PR98 head `4c0b1c2`.
The sibling audit pins were not refreshed.

The audit found no generic untrusted-URL fetcher to consolidate. Website owns
the SSRF-safe per-hop and peer validation path. Wikidata's configured
MediaWiki API transport has different trust, error, and retry contracts;
Description has no equivalent fetcher. The atomic writers overlap
conceptually but have different guarantees: Description fsyncs the file and
parent directory; Wikidata fsyncs text and copy files but not their parent
directory, with different Parquet behavior; Website promotes staged files
with sequential bundle rollback and no fsync. These are not interchangeable
implementations or contracts.

Keep each implementation repository-owned. No shared package, cross-repo
wrapper, or sibling code change is justified by this audit. If a future shared
component is proposed, first identify an owner and agree on the transport
security/error contract or atomic durability/rollback contract, plus shared
contract tests, versioning, release, and vulnerability-fix responsibility.
This review did not modify `osm-polygon-description-tag`,
`osm-polygon-wikidata-only`, `osm-worldcover`, or the excluded geoparser.

### Issue #82 remaining-work ledger

- **CLI split:** the per-stage CLI package, shared option types, and help
  snapshots are implemented in draft PR98. They remain unmerged, so issue #82
  stays open until the PR is merged and the acceptance checks are verified on
  the resulting main branch.
- **Shared fetcher/atomic package:** deferred. The pinned sibling audit found no
  compatible generic fetcher and materially different atomic-write guarantees;
  no extraction work remains justified under the current contracts. Revisit
  only if owners establish a real shared contract and maintainer.
- **Current evidence boundary:** the sibling findings use Description
  `61a053d`, Wikidata `fd3ff315`, and Website `8e36215`. That Website pin
  predates current PR98 head `4c0b1c2e9bc5fc352aa7e69560baf66a78b9c304`.
  The four relevant files were byte-identical at the earlier PR98 head
  `004e7e8f85c83120d4a5dc02b8f2c30f0cae1460`, and a GitHub comparison through
  `4c0b1c2` confirms none changed since. The sibling pins were not refreshed.

### Issue #91 mutation policy and run ledger

The repository keeps its strict per-scope baseline ratchet: new
outside-baseline unverified mutants, timeouts, missing verdicts, and no-test
mutants remain failures. The baseline has 161 explicitly recorded entries.
There is no aggregate or per-area percentage floor. Recommendation: retain this
policy and do not add a 90% floor. Revisit an aggregate or per-area floor only
after healthy scheduled main sweeps establish a stable denominator and the
owner decides one is useful. This preserves the ratchet without inferring a
100% requirement.

Quality run #309 on PR98 head `004e7e8f85c83120d4a5dc02b8f2c30f0cae1460` is
terminal: 62 of 78 jobs succeeded; 15 of 72 mutation shards and `ci-ok` failed.
The shard logs report 134 outside-baseline unverified mutants:

| Mutation shard | Mutants |
| --- | ---: |
| `application.cli.verify` | 3 |
| `application.cli.languages [1/2]` | 12 |
| `application.cli.sentences` | 8 |
| `application.cli.run` | 11 |
| `application.cli.publish` | 4 |
| `pipeline.enrich [1/5]` | 25 |
| `pipeline.enrich [2/5]` | 23 |
| `pipeline.enrich [3/5]` | 18 |
| `pipeline.enrich [4/5]` | 4 |
| `pipeline.enrich [5/5]` | 1 |
| `reporting.verify [1/2]` | 1 |
| `web.politeness` | 10 |
| `web.web_fetch [3/7]` | 2 |
| `web.web_fetch [4/7]` | 11 |
| `web.web_fetch [5/7]` | 1 |

No new baseline entries or equivalence classifications were added. The last
available aggregate score remains run #308's 97.81%; this ledger does not infer
a #309 aggregate score from its shard failures.

Noé's explicit policy decision about whether to add an aggregate or per-area
floor remains open for later review after the scheduled main sweeps have a
healthy, stable denominator. WorldCover's 80% floor and timeout treatment do
not transfer to this repository.

Manifest generation and stable artifact writing look similar across
`osm-worldcover`, but their schemas and atomic-write contracts differ. No
shared runtime helper is justified without an owner-approved contract.
