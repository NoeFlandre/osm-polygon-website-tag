# Duplication review

Reviewed on 2026-10-01 for production logic and test setup in this repository.

## Scan method and limits

A one-off Python AST scan covered `src/osm_polygon_website_tag/**/*.py`,
`scripts/**/*.py`, and `tests/**/*.py`. It removed function docstrings and
source locations, consistently renamed identifiers, parameters, nested
definition names, and keyword labels within each body, then grouped identical
normalized trees containing at least 24 AST nodes. The current worktree scan
counted 1,371 production and 3,037 test function bodies; 1,077 and 2,182,
respectively, met
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
writers target v1.1 and v1.2 schemas; two tiny nested success-fetch recorders
belong to the legacy-shard migration and interrupted/resumed-enrichment tests,
which assert calls at different invocation boundaries; checkpoint tests assert
different polygon and sentence schemas; and language versus text-verification
shard writers use different Arrow schemas. These helpers are small
schema- or scenario-specific fixtures, so extracting them would add
indirection without consolidating substantial behavior. Larger copied
workflow, extraction, remote-client, and receipt setup was moved to shared
helpers, and exact duplicate tests were removed.

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
Website at `8e36215`. The sibling audit pins are historical. In particular,
the Website pin predates the last source-checked published PR98 head
`576183a9eba2b1881809a18e1251cba090d25726`. A GitHub source comparison from
`8e36215` to the audit-time PR98 head `8fb0891a91d642e4129e6208e576b1ef447857d0`
spans five commits and 13 changed paths. Within the reviewed files,
`web/web_fetch.py` changed from blob `b599af46` to
`057f7bac44bd54fbc4a519f6bc0d589c1ec5f9f9`, and
`tests/web/test_web_fetch.py` changed from `7e79d67f` to
`17c60d2073e0487776a27f87ef52e988fa6b8cfd`. Direct current-source checks
confirm the fetcher still validates each request hop with
`validate_public_http_url` and its HTTP transport checks the connected peer
address before sending request bytes. Robots retrieval uses the bounded,
public-address-checked redirect path with recursive robots checks disabled for
that policy request. `storage/atomic.py` and `runtime/run_state.py` remain
byte-identical to the audit pin (`07eb805b` and `d01b0a96`). The current
Website fetcher therefore retains its local SSRF contract, but is not
byte-identical to the audited pin. A direct GitHub comparison from audit-time
PR98 head `8fb0891` to then-current head `576183a` shows one commit and exactly two
changed paths: this review document and the extraction-region regression test.
The fetcher, its tests, and both atomic-writer files therefore remain unchanged
since the source checks at `8fb0891`. The local forward-only candidate prepared
against `576183a` has ten changed paths, all limited to the quality workflow,
benchmark files, project configuration, this report, and benchmark tests;
it does not change the four audited source files. Verify the remote tree after
publication before treating that scope as the final PR tree. The Description
and Wikidata pins were not refreshed.

The audit found no generic untrusted-URL fetcher to consolidate. Website owns
the SSRF-safe per-hop URL and connected-peer validation path. Wikidata's
configured MediaWiki API transport has different trust, error, and retry
contracts; Description has no equivalent fetcher. The atomic writers overlap
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
- **Shared fetcher/atomic package:** deferred. The sibling audit found no
  generic fetcher to consolidate and materially different atomic-write
  guarantees. No shared package, cross-repository wrapper, or sibling code
  change is justified under these contracts. Revisit only if the owners agree
  on a concrete shared contract, contract tests, and a named maintainer.
- **Current evidence boundary:** the sibling findings use Description
  `61a053d`, Wikidata `fd3ff315`, and Website `8e36215`. The Website pin is
  older than the published PR98 head at the last source comparison,
  `576183a9eba2b1881809a18e1251cba090d25726`.
  Comparing the pin to audit-time head `8fb0891` confirmed `web/web_fetch.py`
  and its tests changed, while `storage/atomic.py` and `runtime/run_state.py`
  are byte-identical. A fresh comparison from `8fb0891` to then-current head
  `576183a` found only this review document and
  `tests/pipeline/test_extraction_pipeline.py` changed; the four audited source
  files remain unchanged. The local candidate prepared against `576183a`
  changes ten quality, benchmark, configuration, report, and test paths and
  leaves those four audited files untouched; the remote tree will be verified
  after publication. The fetcher was checked for per-hop URL validation,
  connected-peer validation, and robots-request recursion control. Description
  and Wikidata were not refreshed; findings remain scoped to their audited
  commits.

### Issue #91 mutation policy and run ledger

The repository keeps the strict per-scope baseline ratchet: new outside-baseline
unverified mutants, timeouts, missing verdicts, and no-test mutants remain
failures. The current local baseline contains 161 explicitly recorded mutant
names. There is no aggregate or per-area percentage floor. Recommendation:
retain the ratchet; do not add a 90% floor or infer a 100% requirement. Revisit
a percentage floor only after healthy scheduled main sweeps establish a stable
denominator and the owner decides whether a floor is useful.

The latest available aggregate score remains run #308's 97.81% (9,621/9,836).
PR #97 is still red on its current main-based head `44e2597` (base
`b14f8c6`): run #298 completed 77/88 jobs; ten mutation shards and dependent
`ci-ok` failed. The ten-shard inventory is exact:

| Run #298 shard | Gate finding |
| --- | --- |
| `pipeline.extraction [1/2]` | 1 unverified mutant: `x_extract_pbf__mutmut_20` |
| `publishing.remote_identity [1/4]` | 2 previously baselined mutants now killed: `x__receipt_data_identity__mutmut_13`, `_14` |
| `reporting.card_stats [2/5]` | 1 previously baselined mutant now killed: `x__has_retryable_text_status__mutmut_7` |
| `reporting.card_stats [4/5]` | 1 previously baselined mutant now killed: `x__add_text_stats__mutmut_12` |
| `reporting.geometry_stats [2/5]` | 1 previously baselined mutant now killed: `x__accumulate_shard__mutmut_10` |
| `web.politeness` | 2 unverified mutants: `HostLimiter._wait_for_start__mutmut_13`, `_mutmut_15` |
| `web.text_extract [1/4]` | 18 unverified mutants: `_MetaCharsetParser._record__mutmut_7`; `x__meta_charset_contains_reference__mutmut_{7,9,12,14,15,17,19,20,22,23,24,25,26,27}`; `x__raw_meta_attributes__mutmut_{5,6,8}` |
| `web.text_extract [2/4]` | 7 unverified mutants: `x__xml_byte_pattern_codec__mutmut_{2,4,8,12}`; `x__decode_xml_byte_pattern__mutmut_{9,10,11}` |
| `web.text_extract [3/4]` | 7 unverified mutants: `x__text_codec__mutmut_{11,12}`; `x__xml_declared_decoding__mutmut_{2,11,12,13}`; `x__decode_with_declarations__mutmut_9` |
| `web.web_fetch [5/5]` | 2 previously baselined mutants now killed: `x__download_once__mutmut_{19,20}` |
| **Total** | **10 failed shards; 35 outside-baseline unverified mutants and 7 killed baseline entries** |

Run #314's 78/78 success on PR #98 head `576183a` covers the PR #98 delta
against PR #97. The workflow derives mutation scope from the current PR base;
it does not certify the PR #97 delta that failed in run #298. The safe
integration route is to keep PR #97 unmerged, finish and verify the scoped
PR #98 candidate, then (only with separate approval) retarget PR #98 directly
to current `main` and require its recalculated full quality/mutation matrix to
pass before considering any merge. No branch or PR was retargeted or merged.

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

The complete paginated job list for run #309 contains 78 jobs: 62 succeeded,
15 mutation shards failed, and the dependent `ci-ok` failed. The run's additional
reporting, politeness, and web-fetch failures are reconciled below against the
later mutation replays and hosted results; the failed historical run itself
remains red.

| Run #309 shard | Exact unverified identities | Current evidence and limit |
| --- | --- | --- |
| `reporting.verify [1/2]` | `x__verify_status_artifacts__mutmut_1` | The same identity failed in #311. The omitted-argument case is now exercised by `test_status_artifact_dispatch_uses_the_exact_contract` (`preserve_card_sections=None`); its local replay killed the reporting survivor, and the shard passed in runs #312–#314. |
| `web.politeness` | `HostLimiter.set_host_delay__mutmut_{4,5,7,23,24,26,27}`; `HostLimiter._wait_for_start__mutmut_{13,15,25}` | All except `set_host_delay__mutmut_23` also failed in #311. Current behavior tests cover default and small crawl delays, the first request, spacing, invalid delays, and preservation of retry backoff. The strict current-source politeness shard passed in runs #312–#314. The historical `_23` variant was not individually reapplied after the source refactor, so it is not classified equivalent. |
| `web.web_fetch [3/7]` | `x__follow_redirects__mutmut_5`; `x__apply_robots_policy__mutmut_3` | Exact identities also failed in #311; the current-source replay killed both. See the web-fetch table below for behavior-to-test mapping. |
| `web.web_fetch [4/7]` | `x__robots_policy__mutmut_{8,9,12}`; `x__fetch_robots_policy__mutmut_{25,31}`; `x__robots_fetch_failure__mutmut_17`; `x__parse_robots_policy__mutmut_{2,6,9,12,13}` | These 11 exact identities also failed in #311; the current-source replay killed all 11. See the web-fetch table below. |
| `web.web_fetch [5/7]` | `x__allow_all_robots_parser__mutmut_4` | The exact identity also failed in #311; the current-source replay killed it. See the web-fetch table below. |

The current local regression command was rerun against the focused CLI,
enrichment, reporting, politeness, robots, and web-fetch tests: **535 passed in
9.12 seconds**. This is behavior-suite evidence; it does not replace the
current-head hosted mutation matrix or close any issue by itself.

Run #312, on the prior PR98 head
`f44de41ae33e667a6d38044b1c731d16688eb0f7`, completed at 07:29 UTC with
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

Run #313 on PR98 head `8fb0891a91d642e4129e6208e576b1ef447857d0` passed on
attempt 2: all 78 jobs succeeded, including all 72 mutation shards and
`ci-ok`. Its first attempt had 76/78 jobs pass; `reporting.geometry_stats
[4/5]` timed out downloading `matplotlib==3.11.1` during `uv sync --locked`,
before the mutation gate ran, and `ci-ok` failed while that shard was failed.
Only that geometry-statistics job was retried. The retry passed, reported no
mutation survivors, and did not restart the other successful jobs. Run #313
does not publish an aggregate mutation score artifact; the latest measured
aggregate remains run #308 at 97.81% (9,621/9,836).

A separate extraction replay reproduced the earlier PR97 run #298 survivor
`osm_polygon_website_tag.pipeline.extraction.x_extract_pbf__mutmut_20`: it
replaces the PBF-derived region argument with `None`. The new
`test_extract_persists_region_from_source_filename` asserts the inferred
region in both public and analysis observation shards. The focused test passed
and the exact strict mutation replay killed the mutant. It was absent from both
pull request heads when audited; this follow-up adds it to PR #98. No baseline
entry or equivalence classification was added.

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

The latest targeted regression command covered CLI, enrichment, reporting
verification, politeness, robots policy, and web fetching; all 535 tests passed
in 9.12 seconds. This local regression run does not replace a full mutation
matrix on the next published PR head.

The current policy has no percentage-floor decision awaiting implementation.
The strict ratchet remains in effect, and the next policy review should follow
healthy main-branch observation; passing PR run #313 does not establish the
nightly denominator or observation window. The earlier 80% and 90% floor
suggestions were not adopted and are not current targets; do not add a
percentage floor. WorldCover's threshold and timeout handling do not transfer
to this repository.

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
assembly. Run #313 subsequently passed all seven web-fetch shards and the
politeness shard. The next forward-only PR head still requires its own full
hosted run; these local results do not waive the gate.

### Current status (2026-10-01, after the merges)

PR #99 merged the combined delta of PRs #97 and #98 (merge commit
`f0a55a52f95fb66eff1a5a4ccdbbbd167ee87f8c`) after Quality run #327 passed all
122 jobs on head `2577d30`; PR #103 (merge commit
`d4f9ad9c245756ea1b9d823ed5e7dd8fd376bd02`) followed after run #334. Push runs
on `main` passed (Quality #328 and #335, Documentation #133 and #134) and did not
re-run the mutation matrix. PRs #97 and #98 were closed as superseded by #99.

| Issue | State | What remains |
| --- | --- | --- |
| #65, #68–#74, #76–#81, #83–#85, #87–#89, #92 | Closed by the #99 merge | Nothing, except that #88's private vulnerability-reporting repository setting is an owner/admin action and is not enabled by this change. |
| #82, #86, #90, #94, #95, #96 | Closed after verifying their acceptance criteria on `main` | #86 closes the prospective robots policy only; retroactive treatment of cached text remains an owner decision. |
| #93 | Closed by the #103 merge | Nothing. |
| #91 | Open | The fixed nightly Mutation sweep must run on `main` to confirm baseline-growth failure and the recorded kill ratio. No percentage floor is approved. |
| #75 | Open | Three consecutive green scheduled sweeps and PR Quality p90 under 15 minutes across 20 runs. Both need future runs. |

The `web.web_fetch` mutation baseline is empty: its six reviewed equivalents were
removed by restructuring the code, and the baseline holds 155 entries.

### Historical backlog ledger (pre-merge snapshot): issues #65 and #68–#96

The sections below record the state before PRs #99 and #103 merged and are kept
for provenance; the current status is above.


A fresh GitHub connector read on 2026-10-01 found all 30 requested issues still
open. None has been merged or closed. The live PR #97 body lists 21 issues
under “Closes on merge”; the nine not listed there are #75, #82, #86, #90,
#91, #93, #94, #95, and #96, which remain open follow-ups. Its
current head is `44e25972d5bfe046a06610deee7fdd2a2d977dc4`, and run #298 passed
77 of 88 jobs while 10 mutation shards and `ci-ok` failed. PR #98 is a draft
stacked on PR #97 at remote head `576183a9eba2b1881809a18e1251cba090d25726`.
Run #313 passed all 78 jobs after retrying only the dependency-download
failure in `reporting.geometry_stats [4/5]`. Run #314 completed successfully on
head `576183a`: all 78 jobs passed, including all 72 mutation shards and
`ci-ok`. It produced no aggregate score artifact. PRs #97 and #98 are unmerged,
so no issue is treated as completed on main.

| Issue | Current disposition and remaining acceptance |
| --- | --- |
| #65 | Verified complete in the PR #97 candidate at `44e2597`: no test defines `def _row`, and the cited bare-truthiness assertions are exact. Run #298's quality job passed, and the latest full local coverage suite passes 3,152 tests. PR #97 now lists #65 under closes-on-merge; keep the issue open until that PR is merged. |
| #68 | CLI error handling and exit-code work is in PR #97. Close only after its checks pass and the change is merged. |
| #69 | Global version and verbosity options are in PR #97; help and CLI acceptance remain unmerged. |
| #70 | Read-only `create-repo` preview, explicit apply behavior, option help, and examples are in PR #97; remote-write behavior remains unmerged. |
| #71 | Dependency bounds and update automation are in PR #97; verify the locked audit on the final merge candidate. |
| #72 | Narrower exception handling and error visibility are in PR #97; verify the targeted behavior on the final merge candidate. |
| #73 | The anti-pattern Ruff families are enabled in PR #97 and local lint is green; hosted mutation failures still block the PR. |
| #74 | PR docs, timeouts, audits, and aggregate CI changes are in PR #97; all hosted required checks must pass. |
| #75 | Workflow caching/artifact safeguards and parallel mutation work are in PR #98; run #313 passed after one targeted environment retry, and run #314 passed all 78 jobs on its source-audit head `576183a`. The three-green-night sweep window and 20-run p90-under-15-minute window have not elapsed and cannot be claimed until the fix reaches main. |
| #76 | Complexity/API-shape lint ratchets are in PR #97; final verification and merge remain pending. |
| #77 | Behavioral Docker/docs/MkDocs contract tests are in PR #97; final verification and merge remain pending. |
| #78 | Compose, portable volume/environment conventions, and model support are in PR #97; Docker check passed on run #298, but merge remains pending. |
| #79 | Machine-independent README outputs/schema/citation updates are in PR #97; docs check passed on run #298, but merge remains pending. |
| #80 | Public-doc cleanup and contributor-only AGENTS guidance are in PR #97; docs check passed on run #298, but merge remains pending. |
| #81 | Runtime path cleanup and portable data-root changes are in PR #97; merge remains pending. |
| #82 | The stage-specific CLI package, shared options, and help snapshots are implemented in draft PR #98. At published source-audit head `576183a` (one follow-up commit beyond source-checked head `8fb0891`), all eight Python CLI modules are 248 lines or shorter (largest: `__init__.py`, 248 lines); the prepared forward update leaves the CLI package unchanged. Run #314 passed all 78 jobs on `576183a`; keep open until acceptance is verified on main after merge. Cross-repository extraction is deferred: the audit found no generic fetcher to consolidate and materially different atomic-writer guarantees. Website pin `8e36215` predates PR98; the `8fb0891`→`576183a` comparison changed only the ledger and extraction regression test, and the prepared ten-path follow-up leaves the audited fetcher and atomic-writer source files unchanged. |
| #83 | Declared/BOM/meta charset handling is in PR #97; verify hosted web mutation shards before merging. |
| #84 | Status/header/type checks before body reads are in PR #97; verify hosted web mutation shards before merging. |
| #85 | Per-host concurrency/rate controls and bounded `Retry-After` handling are in PR #97. The politeness shard passed in runs #312, #313, and #314; run #309's earlier failure remains historical. |
| #86 | Prospective robots checks, schema/status handling, and docs are in PR #98. Retroactive treatment of existing cached text remains an owner policy decision; no cache was deleted or reprocessed. Recommendation: retain prospective-only behavior. |
| #87 | Version single-sourcing and release notes/process are in PR #97; final verification and merge remain pending. |
| #88 | Contributor/security docs and templates are in PR #97. Private vulnerability-reporting repository settings were not changed; that separate setting requires owner/admin action if it is part of the acceptance contract. |
| #89 | URL, SSRF, and extraction property tests/profiles are in PR #97; verify the final required checks before merge. |
| #90 | All five local HTTP acceptance scenarios exist in PR #98: extraction, unusable pages, cache hit, interrupted resume, and language population. Verify the zero-outbound-socket guard and full acceptance suite on the final PR head. |
| #91 | The strict ratchet and baseline workflow are in PR #98. All 72 mutation shards and `ci-ok` passed in run #313 attempt 2 after one targeted dependency-download retry; the first attempt's failed setup did not execute mutation tests. Run #314 then passed all 72 mutation shards plus `ci-ok` on head `576183a`; no aggregate score artifact was produced for either run. The latest measured score remains run #308's 97.81% (9,621/9,836). Keep the strict per-scope ratchet; no percentage floor is approved. |
| #92 | CRAP-report hardening is in PR #97. A fresh full coverage run on the current local worktree passed 3,152 tests at 97.12% coverage. The measured report covered all 1,368 functions under `src/` and `scripts/`; the highest score was 5.93 (`domain/geometry.py:_check_antimeridian`), with 0 functions at or above 6. The strict CRAP gate passed; rerun on the final merged candidate. |
| #93 | Loopback behavior tests are in PR #98. The focused web suite passes 255 tests and the latest full local suite passes 3,152. The current-source redirect replay killed 167/168, with its sole survivor matching the already-baselined case-insensitive `Location` header spelling mutant; the separate 200-variant replay killed every one of run #311's 14 exact web-fetch survivors. The previously observed range-loop mutant was removed by a behavior-preserving refactor. All seven run #312 web-fetch shards and all web shards in runs #313 and #314 passed. |
| #94 | Bounded prefetch/process extraction is in PR #98. The latest five-round local stress medians are 7.4597 s flat and 14.2342 s tail against 8 s/15 s targets; both acceptance checks pass. Benchmark jobs in runs #312 and #313 passed; run #314 passed all five enrichment shards. Its two run #312 enrichment survivors have local behavior tests and a passing strict scoped gate. Run #317 reported a 68.8% `test_enrichment_throughput` regression (0.809 s base, 1.366 s head): the spawn-context extraction pool paid about 0.7 s of worker startup per 256-row shard. Workers now start only after 2 MiB of HTML has been extracted inline (`POOL_START_BYTES`), all at once so imports overlap fetching; local medians returned to 0.8–0.9 s and the 25% threshold is unchanged. Local re-measure on 2026-10-01: enrichment throughput medians 0.89–1.13 s; the five #96 benchmark categories (`-m benchmark`) finish in about 10 s against the 60 s target; the CLI imports without Trafilatura in all five samples. Hosted benchmark and the combined-candidate mutation matrix (PR #99) decide the final state. |
| #95 | Lazy Trafilatura loading is in PR #98; five import-time samples recorded a 0.727 s cumulative median with Trafilatura absent after CLI import. Recheck final hosted quality and merge. |
| #96 | PR #97's title no longer claims #96; its body keeps the issue as a follow-up. The prepared PR #98 addition covers all five requested categories under `tests/benchmarks`, including a fixed 110 KiB extraction case; the required command passed 9 tests with 2 stress cases deselected in 18.00 s. The 256-row/20 ms smoke case measured 2.73 s, below its ~5 s target. Separately marked 1,024-row stress medians were 7.4597 s flat and 14.2342 s tail, below the 8 s and 15 s targets. CI runs same-runner comparisons only when `src/**` changes and uses a 25% median threshold; the benchmark check remains advisory. Published head `576183a` predates these benchmark additions, so a successor hosted run must verify the updated candidate. Keep #96 open until the change is merged and the benchmark gate is accepted on main. |

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
## Branch hygiene snapshot (2026-10-01)

Update after the merges: PRs #97 and #98 are closed and #99 and #103 are merged.
Their branches (`claude/repo-access-issues-review-5kjzx6`,
`codex/remaining-followups`, `codex/web-fetch-baseline-and-ratio`) and the two
older merged-PR branches below are kept until the owner approves deleting them.
The text below is the earlier snapshot.

The live GitHub branch listing contains the active branches
`claude/repo-access-issues-review-5kjzx6` (PR #97 head and PR #98 base) and
`codex/remaining-followups` (PR #98 head), plus `main`. Keep all three: both
pull requests remain open and unmerged, and PR #98 is stacked on PR #97. The
only local worktree is `/workspace/osm-polygon-website-tag` on
`codex/remaining-followups`.

Two live branches are cleanup candidates pending the parent bundling explicit
user approval for branch deletion:

- `claude/codebase-quality-audit-jz5aox` at
  `1b4fd8355b9c3c94d81309b3fcf53aa2b2364004`. PRs #66 and #67 are merged; the
  branch tip is exactly PR #67's head. Its tree equals both the PR #67 merge
  commit `b14f8c638f02488691851fd34560cce0fc666425` and `origin/main` at tree
  `3903f3ebe7149850b3fe2f6cd2cd891b03475177`. There is no open PR or worktree
  reference. The commit graph shows 20 branch-only commits because PR #67 was
  squash-merged, but the complete branch tree is present in main.
- `claude/test-suite-cleanup-vxzbu6` at
  `dca81468af212ef2aacef040490ca5eabb1429eb`. PRs #55, #56, and #57 are merged;
  the branch tip is exactly PR #57's head and its tree equals PR #57 merge
  commit `f72ef6ff310fe2a16d2c8c7710ac213313edb37f` at tree
  `337f8c6bbfbdb304a38e4c17a68088e426972b3f`. There is no open PR or worktree
  reference. It is three main commits behind; its merged contents remain
  recoverable from the merge commit.

At the captured branch-list snapshot, the local refs `origin/pr98`,
`origin/pr98-latest`, `origin/pr98-verify-4c0b1c2e`, and `target/pr98` did not
appear in the live GitHub branch listing and had no open PR references. Their
tips `01c59fe7654d`, `4fdd5ad921db`, and `4c0b1c2e9bc5` were ancestors of then-
active PR #98 head `8fb0891a91d642e4129e6208e576b1ef447857d0` (9, 8, and 3
commits ahead, respectively). PR #98 later advanced to `576183a`; this work
has not pruned local recovery refs or deleted remote branches.
