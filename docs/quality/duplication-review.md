# Duplication review

This review was done on 2026-10-01. It covers the production logic and the test setup in this repository.

## Scan method and limits

A one-time Python AST scan covered `src/osm_polygon_website_tag/**/*.py`, `scripts/**/*.py`, and `tests/**/*.py`. The scan did these steps:

1. It removed the function docstrings and the source locations.
2. It renamed, in a consistent way, the identifiers, the parameters, the nested definition names, and the keyword labels in each body.
3. It grouped the identical normalized trees that have 24 AST nodes or more.

The scan of the current worktree counted 1,371 production function bodies and 3,037 test function bodies. Of these, 1,077 and 2,182 met the size cutoff. In these bodies, the scan found three repeated production-body groups (seven bodies) and five repeated test-body groups (10 bodies). The scan counts nested and asynchronous function bodies separately. It normalizes local identifiers, parameters, nested definition names, and keyword labels. It keeps attribute names and schema constants.

These counts show exact structural matches above the size cutoff. They do not prove that the repository has no duplication. The scan does not find fuzzy similarity, repeated module-level setup, or smaller helpers. The reviewer examined each match in context before a code change.

## Consolidated in this change

- `pipeline.time_budget` now owns the rules for the deadline, the remaining time, the exhaustion, and the batch options. Language detection and sentence runs share these rules.
- `reporting.artifact_inventory.source_scoped_parquet_paths` owns the selection of Parquet files that is sorted and filtered by source. Card statistics, geometry statistics, and geographic inputs use it.
- `reporting.geometry_stats.stage_geometry_stats` owns the canonical operation that renders and stages the geometry statistics. Card building and publication both use it.
- `reporting.verify` shares the status-based checks for the analysis, the card, and the receipt. The strict card verification and the release-compatible card verification stay as separate explicit policies.
- `application.source_processing._run_has_shard_needing` now owns the sorted traversal of shards. Enrichment and language detection keep separate predicates.
- `scripts.quality.mutation_gate._mutants_matching` shares the collection of matched names. The verdicts "killed" and "unchecked" use it.
- Workflow tests share offline remote fixtures and static-enrichment fixtures. They also share common synthetic sources and detector helpers. Extraction tests share their XML, PBF-builder, and website-payload setup. Publishing tests now share the setup of a finalized run and the instrumentation of the completion receipt.
- Workflow recovery tests share the extraction tracing. Remote identity tests use a shared read-only Hub API double. CRAP-report tests now use one subprocess helper that takes a path parameter. The reviewer removed a duplicated paused-receipt test and a duplicate empty-shard adapter test. The reviewer first checked that their bodies and inputs were identical to the coverage that stays.

## Intentional local matches

The three repeated production groups that remain are small and specific to a policy:

- The two Grid'5000 sync commands show different language and sentence operations and payloads through Typer. Each command stays explicit. In this way, its options and help text stay with the correct operation.
- Three schema modules show the same small `column_doc` accessor. But each module looks up its own versioned public contract. Each module owns its schema error context.
- The deduplication SQL module and the text-population SQL module each extract one scalar result through their local connection API. Their two-line helpers overlap. This overlap does not justify a shared database abstraction.

The five test matches that remain each keep a separate contract:

- Workflow fakes and language-detection fakes return different predictions.
- Polygon migration writers target the v1.1 and v1.2 schemas.
- Two small nested success-fetch recorders belong to the legacy-shard migration test and to the interrupted/resumed-enrichment test. These tests assert calls at different invocation boundaries.
- Checkpoint tests assert different polygon and sentence schemas.
- The shard writers for language verification and for text verification use different Arrow schemas.

These helpers are small fixtures for one schema or one scenario. If you extract them, you add indirection and you do not consolidate substantial behavior. The reviewer moved the larger copied setup for workflow, extraction, remote-client, and receipt to shared helpers. The reviewer also removed the exact duplicate tests.

## Cross-repository candidates and ownership

A read-only production scan covered this repository and `osm-worldcover`. It found no exact alpha-normalized production function-body clones. Both repositories use a CRAP formula and a strict `<6` limit. But their adapters and their coverage semantics are different. This repository uses coverage.py per-function coverage and fails on missing function entries. WorldCover derives the executed lines and the missing lines through its Radon subprocess adapter. Keep the adapters in the repository that owns them. Change this only when Noé and the maintainers of both repositories agree on these items: the canonical coverage semantics, the dependency ownership, the tests, and a named maintainer for a shared implementation. This review does not propose a shared package.

Issue #82 proposed to share the fetcher of this repository and `storage/atomic.py` with `osm-polygon-description-tag` and `osm-polygon-wikidata-only`. A read-only source audit examined Description at `61a053d`, Wikidata at `fd3ff315`, and Website at `8e36215`. The audit pins of the sibling repositories are historical. The Website pin is older than the last source-checked published PR98 head `576183a9eba2b1881809a18e1251cba090d25726`.

A GitHub source comparison from `8e36215` to the audit-time PR98 head `8fb0891a91d642e4129e6208e576b1ef447857d0` spans five commits and 13 changed paths. In the reviewed files, `web/web_fetch.py` changed from blob `b599af46` to `057f7bac44bd54fbc4a519f6bc0d589c1ec5f9f9`. `tests/web/test_web_fetch.py` changed from `7e79d67f` to `17c60d2073e0487776a27f87ef52e988fa6b8cfd`.

Direct checks of the current source confirm these facts:

- The fetcher still validates each request hop with `validate_public_http_url`.
- Its HTTP transport checks the address of the connected peer before it sends request bytes.
- The retrieval of robots files uses the bounded redirect path that checks public addresses. For that policy request, the recursive robots checks are off.
- `storage/atomic.py` and `runtime/run_state.py` are byte-identical to the audit pin (`07eb805b` and `d01b0a96`).

Thus the current Website fetcher keeps its local SSRF contract. But it is not byte-identical to the audited pin.

A direct GitHub comparison from the audit-time PR98 head `8fb0891` to the then-current head `576183a` shows one commit and exactly two changed paths. The paths are this review document and the extraction-region regression test. Thus the fetcher, its tests, and both atomic-writer files did not change after the source checks at `8fb0891`.

The local forward-only candidate was prepared against `576183a`. It has ten changed paths. All of them are limited to the quality workflow, the benchmark files, the project configuration, this report, and the benchmark tests. It does not change the four audited source files. After publication, verify the remote tree before you treat that scope as the final PR tree. The reviewer did not refresh the Description pin and the Wikidata pin.

The audit found no generic fetcher for untrusted URLs that you can consolidate. Website owns the SSRF-safe validation path (the URL of each hop and the connected peer). The configured MediaWiki API transport of Wikidata has different contracts for trust, errors, and retries. Description has no equivalent fetcher.

The atomic writers overlap in concept, but their guarantees are different:

- Description does fsync on the file and on the parent directory.
- Wikidata does fsync on text files and copy files, but not on their parent directory. Its Parquet behavior is different.
- Website promotes staged files with a sequential bundle rollback and no fsync.

These implementations and contracts are not interchangeable.

Keep each implementation in the repository that owns it. This audit does not justify a shared package, a cross-repository wrapper, or a code change in a sibling. If somebody proposes a shared component in the future, do these steps first:

1. Identify an owner.
2. Agree on the contract for transport security and errors, or on the contract for atomic durability and rollback.
3. Agree on shared contract tests, versioning, release, and who is responsible for vulnerability fixes.

This review did not change `osm-polygon-description-tag`, `osm-polygon-wikidata-only`, `osm-worldcover`, or the excluded geoparser.

### Current status (2026-10-01, after the merges)

PR #99 merged the combined delta of PRs #97 and #98 (merge commit `f0a55a52f95fb66eff1a5a4ccdbbbd167ee87f8c`). Before the merge, Quality run #327 passed all 122 jobs on head `2577d30`. PR #103 (merge commit `d4f9ad9c245756ea1b9d823ed5e7dd8fd376bd02`) followed after run #334. The push runs on `main` passed (Quality #328 and #335, Documentation #133 and #134). They did not run the mutation matrix again. The team closed PRs #97 and #98 as superseded by #99.

| Issue | State | What remains |
| --- | --- | --- |
| #65, #68–#74, #76–#81, #83–#85, #87–#89, #92 | Closed by the #99 merge | Nothing, except that #88's private vulnerability-reporting repository setting is an owner/admin action and is not enabled by this change. |
| #82, #86, #90, #94, #95, #96 | Closed after verifying their acceptance criteria on `main` | #86 closes the prospective robots policy only; retroactive treatment of cached text remains an owner decision. |
| #93 | Closed by the #103 merge | Nothing. |
| #91 | Open | The fixed nightly Mutation sweep must run on `main` to confirm baseline-growth failure and the recorded kill ratio. No percentage floor is approved. |
| #75 | Open | Three consecutive green scheduled sweeps and PR Quality p90 under 15 minutes across 20 runs. Both need future runs. |

On `main`, `docs/quality/mutation-baseline.txt` holds 155 entries and no `web.web_fetch` entry. The team removed its six reviewed equivalents when it restructured the code. The `redirect_step` survivor described below is not a baseline entry.

Treat `main` as the reference for every file, count, and mutant in this review. The ledgers below record the state before the merges of PRs #99 and #103; their references to PR #97 and PR #98 are provenance only. They start at the next heading and continue to the end of the issue sections. They are kept as provenance. The status above replaces their present-tense statements about open pull requests, red runs, and the 161-entry baseline.

### Issue #82 remaining-work ledger

- **CLI split:** The per-stage CLI package, the shared option types, and the help snapshots are done in draft PR98. They are not merged. Thus issue #82 stays open until somebody merges the PR and verifies the acceptance checks on the resulting main branch.
- **Shared fetcher/atomic package:** Deferred. The sibling audit found no generic fetcher to consolidate. It found atomic-write guarantees that are very different. Under these contracts, no shared package, cross-repository wrapper, or sibling code change is justified. Revisit this only if the owners agree on a concrete shared contract, contract tests, and a named maintainer.
- **Current evidence boundary:** The sibling findings use Description `61a053d`, Wikidata `fd3ff315`, and Website `8e36215`. At the last source comparison, the Website pin is older than the published PR98 head `576183a9eba2b1881809a18e1251cba090d25726`. The comparison of the pin to the audit-time head `8fb0891` confirmed that `web/web_fetch.py` and its tests changed. It also confirmed that `storage/atomic.py` and `runtime/run_state.py` are byte-identical. A new comparison from `8fb0891` to the then-current head `576183a` found that only this review document and `tests/pipeline/test_extraction_pipeline.py` changed. The four audited source files did not change. The local candidate was prepared against `576183a`. It changes ten paths (quality, benchmark, configuration, report, and test). It does not touch those four audited files. The reviewer will verify the remote tree after publication. The reviewer checked the fetcher for these items: validation of the URL of each hop, validation of the connected peer, and control of recursion in robots requests. The reviewer did not refresh Description and Wikidata. The findings stay limited to their audited commits.

### Issue #91 mutation policy and run ledger

The repository keeps the strict baseline ratchet for each scope. These items stay failures: new unverified mutants outside the baseline, timeouts, missing verdicts, and mutants with no tests. The baseline on `main` holds 154 mutant names that are explicitly recorded. There is no floor in percent, for the total or for an area. Recommendation: keep the ratchet. Do not add a 90% floor. Do not assume a 100% requirement. Consider a percentage floor again only after healthy scheduled sweeps on main establish a stable denominator and the owner decides that a floor is useful.

The latest available aggregate score is still 97.81% (9,621/9,836) from run #308. PR #97 is still red on its current main-based head `44e2597` (base `b14f8c6`). Run #298 completed 77 of 88 jobs. Ten mutation shards and the dependent `ci-ok` failed. This is the exact inventory of the ten shards:

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

Run #314 passed 78 of 78 jobs on PR #98 head `576183a`. It covers the PR #98 delta against PR #97. The workflow derives the mutation scope from the current PR base. Thus the run does not certify the PR #97 delta that failed in run #298. The safe route to integration has these steps:

1. Keep PR #97 unmerged.
2. Finish and verify the scoped PR #98 candidate.
3. Only with separate approval, retarget PR #98 directly to the current `main`.
4. Require its recalculated full quality/mutation matrix to pass before you consider a merge.

Nobody retargeted or merged a branch or a PR.

Run #309 is the terminal result 62/78 on head `004e7e8f85c83120d4a5dc02b8f2c30f0cae1460`. Fifteen mutation shards and `ci-ok` failed. There were 134 unverified mutants outside the baseline. Run #310 on `4c0b1c2e9bc5fc352aa7e69560baf66a78b9c304` was cancelled before it gave a complete mutation result. The reviewer did not infer a score. Run #311 is a separate terminal result 62/78 on head `f94da44a6b9e61884bc4564df8ed7ca3588cf38f`. It had the same 15 failing shard names plus `ci-ok`. It had 128 unverified mutants outside the baseline. The inventories of the shards differ because mutmut generated different sets of mutations at the two heads:

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

The complete paginated job list for run #309 has 78 jobs. Of these, 62 succeeded, 15 mutation shards failed, and the dependent `ci-ok` failed. The run also had failures in reporting, politeness, and web-fetch. The table below reconciles these failures with the later mutation replays and the hosted results. The historical run itself stays red.

| Run #309 shard | Exact unverified identities | Current evidence and limit |
| --- | --- | --- |
| `reporting.verify [1/2]` | `x__verify_status_artifacts__mutmut_1` | The same identity failed in #311. The test `test_status_artifact_dispatch_uses_the_exact_contract` (`preserve_card_sections=None`) now exercises the case of the omitted argument. Its local replay killed the reporting survivor. The shard passed in runs #312–#314. |
| `web.politeness` | `HostLimiter.set_host_delay__mutmut_{4,5,7,23,24,26,27}`; `HostLimiter._wait_for_start__mutmut_{13,15,25}` | All of them except `set_host_delay__mutmut_23` also failed in #311. The current behavior tests cover these items: default and small crawl delays, the first request, spacing, invalid delays, and the preservation of retry backoff. The strict politeness shard for the current source passed in runs #312–#314. The reviewer did not apply the historical `_23` variant again after the source refactor. Thus the reviewer does not classify it as equivalent. |
| `web.web_fetch [3/7]` | `x__follow_redirects__mutmut_5`; `x__apply_robots_policy__mutmut_3` | The exact identities also failed in #311. The replay on the current source killed both. For the map from behavior to test, see the web-fetch table below. |
| `web.web_fetch [4/7]` | `x__robots_policy__mutmut_{8,9,12}`; `x__fetch_robots_policy__mutmut_{25,31}`; `x__robots_fetch_failure__mutmut_17`; `x__parse_robots_policy__mutmut_{2,6,9,12,13}` | These 11 exact identities also failed in #311. The replay on the current source killed all 11. See the web-fetch table below. |
| `web.web_fetch [5/7]` | `x__allow_all_robots_parser__mutmut_4` | The exact identity also failed in #311. The replay on the current source killed it. See the web-fetch table below. |

The reviewer ran the current local regression command again. It covered the focused tests for CLI, enrichment, reporting, politeness, robots, and web fetch: **535 passed in 9.12 seconds**. This is evidence from the behavior suite. It does not replace the hosted mutation matrix on the current head. It does not close an issue by itself.

Run #312 was on the prior PR98 head `f44de41ae33e667a6d38044b1c731d16688eb0f7`. It completed at 07:29 UTC with the overall conclusion `cancelled`. Of 78 jobs, 74 succeeded. The two enrichment mutation shards failed. The docs job was cancelled. `ci-ok` failed because a required job was cancelled. The docs log says that the operation was cancelled during `uv sync --locked`. The documentation build did not run. The log has no specific reason for the cancellation. Both `reporting.verify` shards, all seven `web.web_fetch` shards, and `web.politeness` passed. Thus the historical failures of run #309 in reporting and web do not occur again on this head.

The complete mutation inventory of run #312 had two survivors outside the baseline:

| Failed shard | Unverified mutants |
| --- | ---: |
| `pipeline.enrich [1/5]` | 1 |
| `pipeline.enrich [3/5]` | 1 |
| **Total** | **2** |

`ci-ok` failed because the required docs job was cancelled. It is not a third mutation failure. The latest aggregate mutation score is still the prior complete result from run #308.

Run #313 was on PR98 head `8fb0891a91d642e4129e6208e576b1ef447857d0`. It passed on attempt 2: all 78 jobs succeeded. This includes all 72 mutation shards and `ci-ok`. In the first attempt, 76 of 78 jobs passed. The job `reporting.geometry_stats [4/5]` timed out when it downloaded `matplotlib==3.11.1` during `uv sync --locked`. This was before the mutation gate ran. `ci-ok` failed while that shard was failed. The team retried only that geometry-statistics job. The retry passed. It reported no mutation survivors. It did not restart the other jobs that succeeded. Run #313 does not publish an aggregate mutation score artifact. The latest aggregate that the team measured is still run #308 at 97.81% (9,621/9,836).

A separate extraction replay reproduced the earlier PR97 run #298 survivor `osm_polygon_website_tag.pipeline.extraction.x_extract_pbf__mutmut_20`. This mutant replaces the region argument that comes from the PBF with `None`. The new test `test_extract_persists_region_from_source_filename` asserts the inferred region in the public shards and in the analysis observation shards. The focused test passed. The exact strict mutation replay killed the mutant. When the reviewer audited the two pull request heads, the mutant was absent from both. This follow-up adds the test to PR #98. The team did not add a baseline entry or an equivalence classification.

These are the two exact hosted mutants: `x__resolve_pending__mutmut_12` and `x_enrich_polygon_shard__mutmut_60`. The local tests now assert the use of a custom extractor without a process pool, and the returned shard path. Both focused tests pass. The strict scoped mutation gate passes with zero baseline hits. The first mutant changes `_record_fetches` to receive `extractor=None`. The second mutant changes the result object to receive `shard_path=None`. The first test asserts the output of the injected custom extractor. The second test asserts that the returned path is equal to the input shard. These local tests are not on the hosted head `f44de41`. In the local strict scoped gate, they kill both exact run #312 survivors. They do not change the terminal hosted result. They do not give an aggregate score.

The local replay evidence is narrower than a new green hosted run. These are the results of the focused replays:

- They killed all 33 CLI survivors and the single reporting survivor from #311.
- They killed six politeness survivors that the source generates now. Three politeness variants from #311 do not exist after the source changes.
- For enrichment, the replay of the 71 filters from #311 first found 14 local survivors among 115 generated variants. Focused reruns and new behavior tests now kill 13 of them. The last form, `missing_ok=True`, was replaced with `staged.unlink()`. This is because `CheckpointStore.assemble` always creates and validates the staged Parquet file before it returns. A full replay of all 71 filters after that final source edit is still pending.

The team completed two focused replays of web mutations on the current source. The redirect replay ran 168 selected mutations. Of these, 167 were killed and one survived. It had no verdicts of `no tests` or timeout. The survivor is `osm_polygon_website_tag.web.web_fetch.x__redirect_step__mutmut_7`. It is not a baseline entry: `main` records no `web.web_fetch` mutant. It changes the spelling of the `Location` lookup to `LOCATION`. `_header` compares header names without case sensitivity. `test_redirect_step_results_exact` covers `Location` and `location`. Thus the mutant keeps the behavior. The team did not add it to the baseline.

A second replay covered 200 selected variants across the redirect, policy-cache, robots-fetch, parser, and allow-all helpers. All 200 were killed. Its scoped strict mutation gate passed. The replay on the current source killed each of the 14 exact web-fetch mutant identities that run #311 reported. These are the specific mutation shapes and the evidence:

| Run #311 shard | Exact hosted mutant identities | Current-source evidence |
| --- | --- | --- |
| `web.web_fetch [3/7]` | `follow_redirects__mutmut_5`; `apply_robots_policy__mutmut_3` | The first changes the bound of the redirect loop to `None`. The tests for the boundary of the redirect limit fail it. The second loses the current redirect URL in a policy error. `test_robots_policy_error_falls_back_to_the_current_redirect_url` fails it. |
| `web.web_fetch [4/7]` | `robots_policy__mutmut_8`, `robots_policy__mutmut_9`, `robots_policy__mutmut_12`; `fetch_robots_policy__mutmut_25`, `fetch_robots_policy__mutmut_31`; `robots_fetch_failure__mutmut_17`; `parse_robots_policy__mutmut_2`, `parse_robots_policy__mutmut_6`, `parse_robots_policy__mutmut_9`, `parse_robots_policy__mutmut_12`, `parse_robots_policy__mutmut_13` | The tests for the cache of each origin, for lock isolation, and for concurrent load catch the changes to the cache, the key, and the lock. Exact assertions of the robots URL catch `None` that passes through the paths for a missing file and for a parse failure. Tests for rules, invalid UTF-8, and a valid crawl-delay catch the changes to the parser and the policy. |
| `web.web_fetch [5/7]` | `allow_all_robots_parser__mutmut_4` | `test_allow_all_robots_parser_allows_every_user_agent_and_path` and the tests for the fallback on a missing or malformed policy fail the mutated parser initialization. |

The earlier loop refactor also removed the unreachable extra-iteration mutation shape that the team saw before. The updated focused web behavior suite passes 255 tests. The full suite passes 3,151 tests. These local results do not change or waive the status of the hosted runs. A new full mutation run on the next PR head is still necessary.

The latest targeted regression command covered CLI, enrichment, reporting verification, politeness, robots policy, and web fetch. All 535 tests passed in 9.12 seconds. This local regression run does not replace a full mutation matrix on the next published PR head.

The current policy has no decision on a percentage floor that waits for implementation. The strict ratchet stays in effect. The next review of the policy must follow a healthy observation period on the main branch. The passing PR run #313 does not establish the denominator of the nightly sweep or the observation window. The team did not adopt the earlier suggestions of an 80% floor and a 90% floor. They are not current targets. Do not add a percentage floor. The threshold and the timeout handling of WorldCover do not apply to this repository.

Seven web-fetch mutants appeared during the review. They are separate from the 14 new web-fetch survivors of run #311. None of them is recorded in the baseline on `main`: the six former entries were removed when the code was restructured, and the seventh (`_redirect_step` 7) was never recorded. The evidence and the status are specific to each mutation:

| Mutation and status | Classification and evidence |
| --- | --- |
| `_coerce_http_value` 18 and 21 (former baseline entries, removed by restructuring) | Both keep the host-side scheme check before the first path slash. The tests reject a `mailto:` value. They accept a colon in a path (`test_coerce_http_value_rejects_a_colon_only_in_the_host_part` and `test_coerce_http_value_adds_a_lowercase_https_scheme`). |
| `_encode_hostname` 4 and 6 (former baseline entries, removed by restructuring) | The two codec spellings produce the same IDNA ASCII form for valid hosts. The normalization tests pin `bücher.example` to its punycode form. The test for an invalid label pins the wrapped error (`test_normalize_http_url_exact`, `test_encode_hostname_wraps_unicode_error`). |
| `_normalise_http_hostname` 11 (former baseline entry, removed by restructuring) | The uppercase `X` that the mutation adds to the `rstrip` character set is unreachable. `SplitResult.hostname` changes the host to lowercase before this call. The normalization tests use an uppercase scheme, an uppercase host, and a trailing dot (`test_normalize_http_url_exact`). |
| `_redirect_step` 7 (survivor, not baselined) | The header lookup does not depend on case. Thus the changed `Location` spelling finds the same header. The exact redirect-step test covers `Location` and `location` (`test_redirect_step_results_exact`). The current scoped replay generated this survivor again. It is not in the baseline on `main`, so a scoped run reports it as outside the baseline. Recording it as equivalent is an owner decision; this review does not add it. |
| `_follow_redirects` 5 (removed by refactor; not baselined) | The mutation added an extra `range` iteration. After `_fetch_step` returned the redirect-limit result, no code path could reach it. Now the loop increments only after a redirect that the code can follow. `test_redirect_limit_exceeded_exact` asserts that the request count stops at the limit. This name is not in the baseline. |

The team did not add a survivor from run #311 to the baseline. The most recent focused web behavior run passed 255 tests. Its scoped mutation replay killed 167 of the 168 selected mutants. The one survivor is the case-spelling `Location` mutant of the redirect step (see above). It is not a baseline entry. The enrichment replay that used only `tests/pipeline/test_enrich.py` gave `no tests` verdicts for mutants that other test files cover. Thus it is not a complete replay of the 71 hosted survivors. Focused enrichment checks killed 13 of the 14 local survivors of the first pass. The last mutation shape does not exist after the team replaced `unlink(missing_ok=True)` with `unlink()`, which follows the validated checkpoint assembly. Run #313 later passed all seven web-fetch shards and the politeness shard. The next forward-only PR head still needs its own full hosted run. These local results do not waive the gate.

### Full backlog ledger: issues #65 and #68–#96

A new read through the GitHub connector on 2026-10-01 found all 30 requested issues still open. Nobody has merged or closed any of them. The live body of PR #97 lists 21 issues under "Closes on merge". The nine issues that are not in that list are #75, #82, #86, #90, #91, #93, #94, #95, and #96. They stay open as follow-ups. The current head of PR #97 is `44e25972d5bfe046a06610deee7fdd2a2d977dc4`. Run #298 passed 77 of 88 jobs. Ten mutation shards and `ci-ok` failed.

PR #98 is a draft stacked on PR #97 at the remote head `576183a9eba2b1881809a18e1251cba090d25726`. Run #313 passed all 78 jobs after the team retried only the failure of the dependency download in `reporting.geometry_stats [4/5]`. Run #314 completed successfully on head `576183a`: all 78 jobs passed, including all 72 mutation shards and `ci-ok`. It produced no aggregate score artifact. PRs #97 and #98 are not merged. Thus the team treats no issue as complete on main.

| Issue | Current disposition and remaining acceptance |
| --- | --- |
| #65 | Verified complete in the PR #97 candidate at `44e2597`: no test defines `def _row`, and the cited bare-truthiness assertions are exact. The quality job of run #298 passed, and the latest full local coverage suite passes 3,152 tests. PR #97 now lists #65 under closes-on-merge; keep the issue open until that PR is merged. |
| #68 | CLI error handling and exit-code work is in PR #97. Close only after its checks pass and the change is merged. |
| #69 | Global version and verbosity options are in PR #97; help and CLI acceptance remain unmerged. |
| #70 | Read-only `create-repo` preview, explicit apply behavior, option help, and examples are in PR #97; remote-write behavior remains unmerged. |
| #71 | Dependency bounds and update automation are in PR #97; verify the locked audit on the final merge candidate. |
| #72 | Narrower exception handling and error visibility are in PR #97; verify the targeted behavior on the final merge candidate. |
| #73 | The anti-pattern Ruff families are enabled in PR #97 and local lint is green; hosted mutation failures still block the PR. |
| #74 | PR docs, timeouts, audits, and aggregate CI changes are in PR #97; all hosted required checks must pass. |
| #75 | Workflow caching/artifact safeguards and parallel mutation work are on `main` (merged with PR #99); run #313 passed after one targeted environment retry, and run #314 passed all 78 jobs on its source-audit head `576183a`. The three-green-night sweep window and 20-run p90-under-15-minute window have not elapsed and need scheduled runs on `main` to elapse before they can be claimed. |
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
| #91 | The strict ratchet and baseline workflow are on `main` (merged with PR #99). All 72 mutation shards and `ci-ok` passed in run #313 attempt 2 after one targeted dependency-download retry; the first attempt's failed setup did not execute mutation tests. Run #314 then passed all 72 mutation shards plus `ci-ok` on head `576183a`; no aggregate score artifact was produced for either run. The latest measured score remains run #308's 97.81% (9,621/9,836). Keep the strict per-scope ratchet; no percentage floor is approved. |
| #92 | CRAP-report hardening is in PR #97. A fresh full coverage run on the current local worktree passed 3,152 tests at 97.12% coverage. The measured report covered all 1,368 functions under `src/` and `scripts/`; the highest score was 5.93 (`domain/geometry.py:_check_antimeridian`), with 0 functions at or above 6. The strict CRAP gate passed; rerun on the final merged candidate. |
| #93 | Loopback behavior tests are in PR #98. The focused web suite passes 255 tests and the latest full local suite passes 3,152. The current-source redirect replay killed 167/168, with its sole survivor matching the already-baselined case-insensitive `Location` header spelling mutant; the separate 200-variant replay killed every one of run #311's 14 exact web-fetch survivors. The previously observed range-loop mutant was removed by a behavior-preserving refactor. All seven run #312 web-fetch shards and all web shards in runs #313 and #314 passed. |
| #94 | Bounded prefetch/process extraction is in PR #98. The latest five-round local stress medians are 7.4597 s flat and 14.2342 s tail against 8 s/15 s targets; both acceptance checks pass. Benchmark jobs in runs #312 and #313 passed; run #314 passed all five enrichment shards. Its two run #312 enrichment survivors have local behavior tests and a passing strict scoped gate. Run #317 reported a 68.8% `test_enrichment_throughput` regression (0.809 s base, 1.366 s head): the spawn-context extraction pool paid about 0.7 s of worker startup per 256-row shard. Workers now start only after 2 MiB of HTML has been extracted inline (`POOL_START_BYTES`), all at once so imports overlap fetching; local medians returned to 0.8–0.9 s and the 25% threshold is unchanged. Local re-measure on 2026-10-01: enrichment throughput medians 0.89–1.13 s; the five #96 benchmark categories (`-m benchmark`) finish in about 10 s against the 60 s target; the CLI imports without Trafilatura in all five samples. Hosted benchmark and the combined-candidate mutation matrix (PR #99) decide the final state. |
| #95 | Lazy Trafilatura loading is in PR #98; five import-time samples recorded a 0.727 s cumulative median with Trafilatura absent after CLI import. Recheck final hosted quality and merge. |
| #96 | PR #97's title no longer claims #96; its body keeps the issue as a follow-up. The prepared PR #98 addition covers all five requested categories under `tests/benchmarks`, including a fixed 110 KiB extraction case; the required command passed 9 tests with 2 stress cases deselected in 18.00 s. The 256-row/20 ms smoke case measured 2.73 s, below its ~5 s target. Separately marked 1,024-row stress medians were 7.4597 s flat and 14.2342 s tail, below the 8 s and 15 s targets. CI runs same-runner comparisons only when `src/**` changes and uses a 25% median threshold; the benchmark check remains advisory. Published head `576183a` predates these benchmark additions, so a successor hosted run must verify the updated candidate. Keep #96 open until the change is merged and the benchmark gate is accepted on main. |

For #86, these are the choices for the owner and their consequences:

- **Prospective-only (recommended, current behavior):** The robots rules govern new fetch requests. Cached text that succeeded stays eligible in later builds. This keeps the existing snapshot rows. It does not need a new crawl or a deletion of data.
- **Soft-exclude existing cached text:** Keep the bytes, but omit the matching text from rebuilt outputs. This changes the dataset rows and the card statistics. You must define how the current robots responses map to the historical crawls.
- **Retroactive refetch or quarantine/deletion:** Crawl again every origin that is still allowed, or remove or quarantine the text that is now disallowed. This changes the eligibility and the reproducibility of the dataset. It needs network work. A deletion needs a separate explicit approval. Nobody has deleted data.

For #91, keep the strict baseline ratchet for each scope. Do not add a 90% floor. The only future decision for the owner is whether a percentage floor adds value after the nightly and PR observation windows establish a stable denominator. The current code work does not need this decision first.

## Branch hygiene snapshot (2026-10-01)

Update after the merges: PRs #97 and #98 are closed. PRs #99 and #103 are merged. Their branches (`claude/repo-access-issues-review-5kjzx6`, `codex/remaining-followups`, `codex/web-fetch-baseline-and-ratio`) and the two older merged-PR branches below stay until the owner approves their deletion. The text below is the earlier snapshot.

The live GitHub branch listing has the active branches `claude/repo-access-issues-review-5kjzx6` (PR #97 head and PR #98 base) and `codex/remaining-followups` (PR #98 head), and `main`. Keep all three. Both pull requests are open and not merged. PR #98 is stacked on PR #97. The only local worktree is `/workspace/osm-polygon-website-tag` on `codex/remaining-followups`.

Two live branches are candidates for cleanup. The parent must first get explicit approval from the user to delete branches:

- `claude/codebase-quality-audit-jz5aox` at `1b4fd8355b9c3c94d81309b3fcf53aa2b2364004`. PRs #66 and #67 are merged. The branch tip is exactly the head of PR #67. Its tree is equal to the PR #67 merge commit `b14f8c638f02488691851fd34560cce0fc666425` and to `origin/main` at tree `3903f3ebe7149850b3fe2f6cd2cd891b03475177`. No open PR or worktree refers to it. The commit graph shows 20 commits that only the branch has, because the team squash-merged PR #67. But the complete tree of the branch is in main.
- `claude/test-suite-cleanup-vxzbu6` at `dca81468af212ef2aacef040490ca5eabb1429eb`. PRs #55, #56, and #57 are merged. The branch tip is exactly the head of PR #57. Its tree is equal to the PR #57 merge commit `f72ef6ff310fe2a16d2c8c7710ac213313edb37f` at tree `337f8c6bbfbdb304a38e4c17a68088e426972b3f`. No open PR or worktree refers to it. It is three main commits behind. You can recover its merged contents from the merge commit.

At the captured snapshot of the branch list, the local refs `origin/pr98`, `origin/pr98-latest`, `origin/pr98-verify-4c0b1c2e`, and `target/pr98` were not in the live GitHub branch listing. No open PR referred to them. Their tips `01c59fe7654d`, `4fdd5ad921db`, and `4c0b1c2e9bc5` were ancestors of the then-active PR #98 head `8fb0891a91d642e4129e6208e576b1ef447857d0` (9, 8, and 3 commits ahead). PR #98 later advanced to `576183a`. This work did not prune the local recovery refs and did not delete remote branches.
