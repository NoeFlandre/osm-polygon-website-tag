# Duplication review

Reviewed on 2026-10-01 for production logic and test setup in this repository.

## Scan method and limits

A one-off Python AST scan covered `src/osm_polygon_website_tag/**/*.py`,
`scripts/**/*.py`, and `tests/**/*.py`. It removed function docstrings and
source locations, consistently renamed identifiers, parameters, nested
definition names, and keyword labels within each body, then grouped identical
normalized trees containing at least 24 AST nodes. It counted 1,370
production and 3,006 test function bodies; 1,076 and 2,161, respectively, met
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
`f44de41ae33e667a6d38044b1c731d16688eb0f7`. The current-source comparison
shows `web/web_fetch.py` and `tests/web/test_web_fetch.py` changed after the
audit pin, while `storage/atomic.py` and `runtime/run_state.py` remain
byte-identical (`07eb805b` and `d01b0a96`). The fetcher changed from blob
`b599af46` to `057f7bac`; its tests changed from `7e79d67f` to `17c60d20`.
The PR98 change adds robots checks around the redirect loop. The current
implementation still validates every request hop with
`validate_public_http_url`, and its HTTP transport checks the connected peer
address before sending request bytes. Robots retrieval goes through the same
bounded redirect and public-address path with recursive robots checks disabled
for that policy request. Thus the changed Website path retains its local SSRF
contract, but is not byte-identical to the audited pin. The sibling audit pins
were not refreshed.

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
  predates current PR98 head `f44de41ae33e667a6d38044b1c731d16688eb0f7`.
  `web/web_fetch.py` and its tests changed after the audit pin; the current
  source was checked for per-hop URL validation, connected-peer validation,
  and robots-request recursion control. `storage/atomic.py` and
  `runtime/run_state.py` remain byte-identical. The sibling repository pins
  were not refreshed.

### Issue #91 mutation policy and run ledger

The repository keeps the strict per-scope baseline ratchet: new outside-baseline
unverified mutants, timeouts, missing verdicts, and no-test mutants remain
failures. The current local baseline contains 161 explicitly recorded mutant
names. There is no aggregate or per-area percentage floor. Recommendation:
retain the ratchet; do not add a 90% floor or infer a 100% requirement. Revisit
a percentage floor only after healthy scheduled main sweeps establish a stable
denominator and the owner decides whether a floor is useful.

The latest available aggregate score remains run #308's 97.81% (9,621/9,836).
Run #309 is the terminal 62/78 result on head
`004e7e8f85c83120d4a5dc02b8f2c30f0cae1460`: 15 mutation shards plus `ci-ok`
failed, with 134 outside-baseline unverified mutants. Run #310 on
`4c0b1c2e9bc5fc352aa7e69560baf66a78b9c304` was cancelled before a complete
mutation result; no score is inferred. Run #311 is a separate terminal 62/78
result on head `f94da44a6b9e61884bc4564df8ed7ca3588cf38f`; it had the same 15
failing shard names plus `ci-ok`, with 128 outside-baseline unverified
mutants. The per-shard inventories differ as mutmut generated different
mutation sets at the two heads:

| Mutation shard | Run #309 (`004e7e8`) | Run #311 (`f94da44`) |
| --- | ---: | ---: |
| `application.cli.publish` | 4 | 4 |
| `application.cli.verify` | 3 | 3 |
| `application.cli.run` | 11 | 6 |
| `application.cli.languages [1/2]` | 12 | 12 |
| `application.cli.sentences` | 8 | 8 |
| `pipeline.enrich [1/5]` | 25 | 23 |
| `pipeline.enrich [2/5]` | 23 | 4 |
| `pipeline.enrich [3/5]` | 18 | 25 |
| `pipeline.enrich [4/5]` | 4 | 18 |
| `pipeline.enrich [5/5]` | 1 | 1 |
| `reporting.verify [1/2]` | 1 | 1 |
| `web.politeness` | 10 | 9 |
| `web.web_fetch [3/7]` | 2 | 2 |
| `web.web_fetch [4/7]` | 11 | 11 |
| `web.web_fetch [5/7]` | 1 | 1 |
| **Total** | **134** | **128** |

Run #312 is the current hosted run on PR98 head
`f44de41ae33e667a6d38044b1c731d16688eb0f7`. It completed at 07:29 UTC with
overall conclusion `cancelled`: 74 of 78 jobs succeeded; the two enrichment
mutation shards failed, the docs job was cancelled, and `ci-ok` failed because
a required job was cancelled. The docs log says the operation was cancelled
during `uv sync --locked`; the documentation build was skipped and the log
contains no specific cancellation reason. Both `reporting.verify` shards, all
seven `web.web_fetch` shards, and `web.politeness` passed, so the historical
run #309 reporting and web failures do not recur on this head.

Run #312's complete mutation inventory had two outside-baseline survivors:

| Failed shard | Unverified mutants |
| --- | ---: |
| `pipeline.enrich [1/5]` | 1 |
| `pipeline.enrich [3/5]` | 1 |
| **Total** | **2** |

`ci-ok` failed because the required docs job was cancelled; it is not a third
mutation failure. The latest aggregate mutation score remains the prior
complete result from run #308.

The two exact hosted mutants are
`x__resolve_pending__mutmut_12` and
`x_enrich_polygon_shard__mutmut_60`. Local tests now assert custom-extractor
use without a process pool and the returned shard path; both focused tests
pass, and the strict scoped mutation gate passes with zero baseline hits. These
mutants change `_record_fetches` to receive `extractor=None` and the result
object to receive `shard_path=None`, respectively. The first test asserts the
injected custom extractor's output; the second asserts the returned path equals
the input shard. These local tests are not present on hosted head `f44de41`.
They kill both exact run #312 survivors in the local strict scoped gate, but do
not alter the terminal hosted result or produce an aggregate score.

The current local replay evidence is narrower than a fresh hosted green run.
Focused replays killed all 33 CLI survivors and the single reporting survivor
from #311. Six currently generated politeness survivors were killed; three
#311 politeness variants no longer exist after source changes. For enrichment,
the replay of the 71 #311 filters initially found 14 local survivors among 115
generated variants. Focused reruns and new behavior tests now kill 13 of those;
the last `missing_ok=True` form was replaced with `staged.unlink()` because
`CheckpointStore.assemble` always creates and validates the staged Parquet file
before returning. A full replay of all 71 filters after that final source edit
remains pending.

Two focused current-source web mutation replays were completed. The redirect
replay ran 168 selected mutations: 167 were killed and one survived; it had no
`no tests` or timeout verdicts. That survivor is the exact existing baseline
entry `osm_polygon_website_tag.web.web_fetch.x__redirect_step__mutmut_7`, which
changes the `Location` lookup spelling to `LOCATION`. `_header` compares header
names case-insensitively, and `test_redirect_step_results_exact` covers both
`Location` and `location`; the mutant therefore preserves behavior. The
baseline entry was already present and was not broadened.

A second replay covered 200 selected variants across redirect, policy-cache,
robots-fetch, parser, and allow-all helpers; all 200 were killed. Its scoped
strict mutation gate passed. Each of the 14 exact web-fetch mutant identities
reported by run #311 was killed by the current-source replay, with these
specific mutation shapes and evidence:

| Run #311 shard | Exact hosted mutant identities | Current-source evidence |
| --- | --- | --- |
| `web.web_fetch [3/7]` | `follow_redirects__mutmut_5`; `apply_robots_policy__mutmut_3` | The first changes the redirect loop bound to `None`; redirect-limit boundary tests fail it. The second loses the current redirect URL in a policy error; `test_robots_policy_error_falls_back_to_the_current_redirect_url` fails it. |
| `web.web_fetch [4/7]` | `robots_policy__mutmut_8`, `robots_policy__mutmut_9`, `robots_policy__mutmut_12`; `fetch_robots_policy__mutmut_25`, `fetch_robots_policy__mutmut_31`; `robots_fetch_failure__mutmut_17`; `parse_robots_policy__mutmut_2`, `parse_robots_policy__mutmut_6`, `parse_robots_policy__mutmut_9`, `parse_robots_policy__mutmut_12`, `parse_robots_policy__mutmut_13` | Per-origin cache, lock isolation, and concurrent-load tests catch cache/key/lock changes. Exact robots URL assertions catch `None` passed through missing-file and parse-failure paths. Rules, invalid UTF-8, and valid crawl-delay tests catch parser and policy changes. |
| `web.web_fetch [5/7]` | `allow_all_robots_parser__mutmut_4` | `test_allow_all_robots_parser_allows_every_user_agent_and_path` and the missing/malformed-policy fallback tests fail the mutated parser initialization. |

The earlier loop refactor also removed the previously observed unreachable
extra-iteration mutation shape. The updated focused web behavior suite passes
255 tests, and the full suite passes 3,151 tests. These local results do not
change or waive the hosted run statuses; a fresh full mutation run on the next
PR head is still required.

The current policy has no percentage-floor decision awaiting implementation.
The strict ratchet remains in effect, and the next policy review should follow
healthy main-branch observation rather than this failed PR run. The earlier
80% and 90% suggestions were proposals only; WorldCover's threshold and timeout
handling do not transfer to this repository.

Seven web-fetch mutants surfaced during review, separate from run #311's 14
new web-fetch survivors. Six are explicitly recorded in the strict baseline;
the seventh was not added. Their evidence and status are specific to each
mutation:

| Mutation and status | Classification and evidence |
| --- | --- |
| `_coerce_http_value` 18 and 21 (baseline) | Both preserve the host-side scheme check before the first path slash. The tests reject a `mailto:` value while accepting a colon in a path (`test_coerce_http_value_rejects_a_colon_only_in_the_host_part` and `test_coerce_http_value_adds_a_lowercase_https_scheme`). |
| `_encode_hostname` 4 and 6 (baseline) | The two codec spellings produce the same IDNA ASCII form for valid hosts. Normalization tests pin `bücher.example` to its punycode form; the invalid-label test pins the wrapped error (`test_normalize_http_url_exact`, `test_encode_hostname_wraps_unicode_error`). |
| `_normalise_http_hostname` 11 (baseline) | The added uppercase `X` in the `rstrip` character set is unreachable because `SplitResult.hostname` lowercases the host before this call. Normalization tests exercise an uppercase scheme/host and a trailing dot (`test_normalize_http_url_exact`). |
| `_redirect_step` 7 (baseline) | Header lookup is case-insensitive, so the changed `Location` spelling resolves the same header. The exact redirect-step test covers both `Location` and `location` (`test_redirect_step_results_exact`). The current scoped replay regenerated this exact pre-existing baseline survivor; it was not reclassified or added again. |
| `_follow_redirects` 5 (removed by refactor; not baselined) | The mutation added a redundant `range` iteration that could not be reached after `_fetch_step` returned the redirect-limit result. The loop now increments only after a followable redirect; `test_redirect_limit_exceeded_exact` asserts the request count stops at the limit. This name is absent from the baseline. |

No run #311 survivor was added to the baseline. The most recent focused web
behavior run passed 255 tests; its scoped mutation replay killed 167 of 168
selected mutants, with the single survivor matching the existing, specifically
documented case-insensitive-header baseline entry above. The enrichment replay
using only `tests/pipeline/test_enrich.py` produced `no tests` verdicts for
mutants covered outside that test file, so it is not a complete replay of the
71 hosted survivors. Focused enrichment checks killed 13 of the 14 first-pass
local survivors; the last mutation shape no longer exists after replacing
`unlink(missing_ok=True)` with `unlink()` following validated checkpoint
assembly. A fresh hosted run is still required to verify the updated web
shards. These local results do not change the failed hosted status or relax the
gate.

### Full backlog ledger: issues #65 and #68–#96

A fresh GitHub connector read on 2026-10-01 found all 30 requested issues still
open. None has been merged or closed. PR #97 lists 20 as “Closes on merge”; its
current head is `44e25972d5bfe046a06610deee7fdd2a2d977dc4`, and run #298 passed
77 of 88 jobs while 10 mutation shards and `ci-ok` failed. PR #98 is a draft
stacked on PR #97 at remote head `f44de41ae33e667a6d38044b1c731d16688eb0f7`.
Run #312 completed with a cancelled overall conclusion, two enrichment
mutation failures, a cancelled docs job, and failed `ci-ok`. PRs #97 and #98
are unmerged, so no issue is treated as completed on main.

| Issue | Current disposition and remaining acceptance |
| --- | --- |
| #65 | Partially addressed by shared test-fixture work in PRs #97/#98. Finish adopting the versioned row builders and tightening the exact weak assertions; the PR itself still lists this as a follow-up. |
| #68 | CLI error handling and exit-code work is in PR #97. Close only after its checks pass and the change is merged. |
| #69 | Global version and verbosity options are in PR #97; help and CLI acceptance remain unmerged. |
| #70 | Read-only `create-repo` preview, explicit apply behavior, option help, and examples are in PR #97; remote-write behavior remains unmerged. |
| #71 | Dependency bounds and update automation are in PR #97; verify the locked audit on the final merge candidate. |
| #72 | Narrower exception handling and error visibility are in PR #97; verify the targeted behavior on the final merge candidate. |
| #73 | The anti-pattern Ruff families are enabled in PR #97 and local lint is green; hosted mutation failures still block the PR. |
| #74 | PR docs, timeouts, audits, and aggregate CI changes are in PR #97; all hosted required checks must pass. |
| #75 | Workflow caching/artifact safeguards and parallel mutation work are in PR #98. The three-green-night sweep window and 20-run p90-under-15-minute window have not elapsed and cannot be claimed until the fix reaches main. |
| #76 | Complexity/API-shape lint ratchets are in PR #97; final verification and merge remain pending. |
| #77 | Behavioral Docker/docs/MkDocs contract tests are in PR #97; final verification and merge remain pending. |
| #78 | Compose, portable volume/environment conventions, and model support are in PR #97; Docker check passed on run #298, but merge remains pending. |
| #79 | Machine-independent README outputs/schema/citation updates are in PR #97; docs check passed on run #298, but merge remains pending. |
| #80 | Public-doc cleanup and contributor-only AGENTS guidance are in PR #97; docs check passed on run #298, but merge remains pending. |
| #81 | Runtime path cleanup and portable data-root changes are in PR #97; merge remains pending. |
| #82 | Stage-specific CLI modules and help snapshots are implemented in PR #98; the largest current CLI module is 248 lines. The sibling audit found no compatible shared fetcher or atomic writer, so cross-repository extraction is deferred. Verify after merge on main. |
| #83 | Declared/BOM/meta charset handling is in PR #97; verify hosted web mutation shards before merging. |
| #84 | Status/header/type checks before body reads are in PR #97; verify hosted web mutation shards before merging. |
| #85 | Per-host concurrency/rate controls and bounded `Retry-After` handling are in PR #97; the prior politeness shard failed, while the run #312 politeness mutation shard passed. |
| #86 | Prospective robots checks, schema/status handling, and docs are in PR #98. Retroactive treatment of existing cached text remains an owner policy decision; no cache was deleted or reprocessed. Recommendation: retain prospective-only behavior. |
| #87 | Version single-sourcing and release notes/process are in PR #97; final verification and merge remain pending. |
| #88 | Contributor/security docs and templates are in PR #97. Private vulnerability-reporting repository settings were not changed; that separate setting requires owner/admin action if it is part of the acceptance contract. |
| #89 | URL, SSRF, and extraction property tests/profiles are in PR #97; verify the final required checks before merge. |
| #90 | All five local HTTP acceptance scenarios exist in PR #98: extraction, unusable pages, cache hit, interrupted resume, and language population. Verify the zero-outbound-socket guard and full acceptance suite on the final PR head. |
| #91 | The strict ratchet and baseline workflow are in PR #98; terminal run #312 has two failed enrichment mutation shards and no aggregate mutation score. Keep the current strict policy; no percentage floor is approved. |
| #92 | CRAP-report hardening is in PR #97. The fresh full measured report for this candidate covered 1,368 functions at 97.12% coverage; highest CRAP was 5.93 (`domain/geometry.py:_check_antimeridian`), with zero functions at or above 6. The strict CRAP gate passed; rerun on the final merged candidate. |
| #93 | Loopback behavior tests are in PR #98. The focused web suite passes 255 tests and the full suite passes 3,151. The current-source redirect replay killed 167/168, with its sole survivor matching the already-baselined case-insensitive `Location` header spelling mutant; the separate 200-variant replay killed every one of run #311's 14 exact web-fetch survivors. The previously observed range-loop mutant was removed by a behavior-preserving refactor. All seven run #312 web-fetch shards passed. |
| #94 | Bounded prefetch/process extraction is in PR #98. Recorded strict benchmark medians are 6.9203 s flat and 13.9131 s tail against 8 s/15 s targets; run #312's benchmark job passed. Its two enrichment mutation survivors have local behavior tests and a passing strict scoped gate, but require a new hosted run. |
| #95 | Lazy Trafilatura loading is in PR #98; five import-time samples recorded a 0.727 s cumulative median with Trafilatura absent after CLI import. Recheck final hosted quality and merge. |
| #96 | PR #97 marks this issue for closure on merge, but also lists it as a follow-up; reconcile that description before merge. The benchmark suite/regression comparison is implemented and benchmark jobs passed on runs #298, #311, and #312. |

For #86, the owner-facing choices and consequences are:

- **Prospective-only (recommended, current behavior):** robots rules govern new
  fetch requests; successful cached text remains eligible in later builds.
  This preserves existing snapshot rows and avoids recrawling or deleting data.
- **Soft-exclude existing cached text:** retain the bytes but omit matching text
  from rebuilt outputs. This changes dataset rows and card statistics and
  requires defining how current robots responses map to historical crawls.
- **Retroactive refetch or quarantine/deletion:** recrawl every still-allowed
  origin or remove/quarantine text that is now disallowed. This changes dataset
  eligibility and reproducibility, requires network work, and deletion needs a
  separate explicit approval. No data has been deleted.

For #91, retain the strict per-scope baseline ratchet. Do not add a 90% floor.
The only future owner decision is whether a percentage floor would add value
after the nightly and PR observation windows establish a stable denominator;
that decision is not a prerequisite for the current code work.
