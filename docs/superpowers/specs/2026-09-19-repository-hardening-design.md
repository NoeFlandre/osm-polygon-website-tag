# Website-tag Repository Hardening Design

## Goal

Bring the repository from the current release-preparation state to a merged,
reviewed, reproducible, and maintainable state without changing the published
dataset semantics or deleting useful provenance.

## Current constraints

- The public Hugging Face snapshot is already published and must remain the
  behavioural reference for release metadata and aggregate counts.
- The working repository has user-owned untracked `reports/`, `site/`, and
  `slides/` artifacts. They are outside this change and must not be cleaned or
  added accidentally.
- Issues #6--#12 are the active acceptance backlog. Issues #6--#9 describe
  release and quality work already partly implemented; #10--#12 describe
  modularity and test-maintenance work still required.
- GitHub pull-request discussion is audit history and cannot be safely deleted.
  Obsolete branches may be deleted only after their commits are merged or
  otherwise proven unnecessary.

## Architecture

The release flow remains artifact-derived:

1. reporting reducers read the selected run's published shards;
2. dedicated statistics modules compute text and geometry summaries;
3. card and map renderers consume those summaries;
4. publishing validates receipt, source/data identity, and the five public
   metadata files before upload or no-op completion.

The refactor makes those boundaries explicit. `reporting.card` owns markdown
and YAML rendering only. Release staging and promotion move to
`publishing`. Existing `card_stats` and `geometry_stats` remain the owners of
aggregate computation. Public callables keep compatibility wrappers where
needed so callers do not change during the migration.

## Correctness contract

The following behaviours are preserved and tested:

- global unique text identities are keyed by `(osm_type, osm_id)`;
- map, card, report, and `stats.json` use the same trimmed successful-text
  population;
- geometry statistics use every published polygon row and remain serial where
  floating-point order affects reproducibility;
- legacy receipts remain readable while new receipts bind manifests and
  remote Parquet identities;
- card custom content and trusted metadata survive refresh;
- the exact five-file release set is validated, uploaded, independently
  reread, and recognized as a no-op when bytes already match;
- the current published release's observable values remain unchanged.

Before structural edits, the test suite is collected and the focused release,
card, reporting, architecture, and quality results are recorded. After edits,
the same collection count and every existing test name must remain present.

## Refactor and cleanup scope

1. Finish the nested-package mutation mapping and runner safeguards from #9.
   Mutation scope must stay source-limited and thresholds must not be weakened.
2. Move release artifact promotion out of `reporting/card.py` and retain
   compatibility entry points while updating tests and architecture rules.
3. Keep statistics in `card_stats.py` and `geometry_stats.py`; reduce the card
   renderer to presentation and metadata-preservation responsibilities.
4. Split oversized test modules by concern, centralize repeated fixtures, use
   pytest-managed temporary paths, and preserve every test name and collection
   count.
5. Hoist all first-party imports out of function bodies, verify standalone
   imports, and enable Ruff's import-outside-top-level rule if compatible with
   the existing configuration.
6. Remove only tracked files, configuration, or documentation that is proven
   unused, superseded, or contradictory. Keep public provenance, release
   receipts, license/citation files, and valid operational documentation.
7. Keep performance improvements measurement-driven: retain the existing
   spill-backed global reducer, single-pass digest cache, and serial geometry
   aggregate; add no speculative parallelism that can change bytes or exhaust
   memory.

## Verification and delivery

Each change is developed with a focused regression test before implementation,
then the complete gates are run:

```text
just check
just pre-commit
just pre-push
just qa-gauntlet
uv run --locked mkdocs build --strict
```

The CI mutation shards, Docker smoke test, and review findings must be green
or explicitly resolved before merge. The branch is then pushed to PR #13,
issues are closed only with links to evidence, and the two already-merged
remote branches are removed. The release branch is removed only after its
contents are merged. The final report records commit SHAs, check URLs, issue
closures, branch cleanup, and the unchanged Hugging Face revision unless a
new release is explicitly required by a verified output change.
