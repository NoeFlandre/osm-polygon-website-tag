set dotenv-load := false
set export

# Mutant runs are independent processes, so the sweep scales with cores. CI
# runners have four; a workstation usually has more.
MUTATION_CHILDREN := env("MUTATION_CHILDREN", "4")

UV_CACHE_DIR := if env("UV_CACHE_DIR", "") != "" {
    env("UV_CACHE_DIR", "")
} else if path_exists("/Volumes/Seagate M3/projects/osm-polygon-website-tag") == "true" {
    "/Volumes/Seagate M3/projects/osm-polygon-website-tag/uv-cache"
} else {
    "/tmp/osm-polygon-website-tag-uv-cache"
}

BUILD_OUTPUT_DIR := if path_exists("/Volumes/Seagate M3/projects/osm-polygon-website-tag") == "true" {
    "/Volumes/Seagate M3/projects/osm-polygon-website-tag/build"
} else {
    "dist"
}

default: check

sync:
    uv sync --locked

lock:
    uv lock --check

lint:
    uv run --locked ruff check .

ruff:
    uv run --locked ruff check .
    uv run --locked ruff format --check .

format:
    uv run --locked ruff check . --fix
    uv run --locked ruff format .

format-check:
    uv run --locked ruff format --check .

baseline:
    git rev-parse --abbrev-ref HEAD
    git status --short --branch
    uv lock --check

typecheck:
    uv run --locked ty check src tests scripts

test:
    uv run --locked pytest -n auto

unit:
    uv run --locked pytest -n auto tests --ignore=tests/acceptance --ignore=tests/architecture

acceptance:
    uv run --locked pytest -n auto tests/acceptance

architecture:
    uv run --locked pytest tests/architecture

# Verify and recompute the card and statistics report without uploading.
release-stats-dry-run run_dir:
    uv run --locked osm-polygon-website-tag release-stats \
        --run-dir "{{ run_dir }}" \
        --confirm-repo 'NoeFlandre/osm-polygon-website-tag'

# Recompute, publish, and verify only the card and statistics report.
release-stats run_dir:
    uv run --locked osm-polygon-website-tag release-stats \
        --run-dir "{{ run_dir }}" \
        --confirm-repo 'NoeFlandre/osm-polygon-website-tag' \
        --apply

build:
    uv build --out-dir "{{ BUILD_OUTPUT_DIR }}"

check: lock lint format-check typecheck test build

pre-commit:
    uv run --locked pre-commit run --all-files

pre-push:
    uv run --locked pre-commit run --all-files --hook-stage pre-push

COVERAGE_JSON := env("COVERAGE_JSON", "/tmp/osm-polygon-website-tag-coverage.json")

# One instrumented run of the whole suite; downstream gates read its artifact
# rather than paying for the suite again. Sharded across cores: the project
# lives on an external volume whose read latency, not CPU, sets the pace, so
# the serial suite spent most of its eighteen minutes waiting on I/O.
# Tier 3/4: the single source of test and coverage truth.
coverage:
    uv run --locked pytest -n auto --cov=osm_polygon_website_tag --cov-report=term-missing --cov-report=json:"{{ COVERAGE_JSON }}" --cov-fail-under=75

# Depends on nothing so CI never runs the suite twice; run `just coverage`
# first, or use `just qa-pr`, which sequences them.
# Tier 3/4: CRAP gate over the existing coverage artifact.
crap:
    uv run --locked python scripts/quality/crap_report.py --coverage-json "{{ COVERAGE_JSON }}" --path src/osm_polygon_website_tag --max-crap 6

mutation: mutation-clean
    uv run --locked python scripts/quality/mutation_runner.py run --max-children "{{ MUTATION_CHILDREN }}"
    just mutation-gate

# Results persist in the mutant workspace between invocations, so a run must
# start from a clean one or the gate reads verdicts for mutants it never
# checked.
mutation-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf mutants .pytest_cache
    # Bytecode caches record the absolute path they were compiled at; a stale
    # one copied into the workspace fails collection with a missing directory.
    find src tests scripts -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    find src tests scripts -name '*.pyc' -delete 2>/dev/null || true

# Mutate only the package modules a change touches. A full sweep is hours of
# work that a hosted runner does not reliably survive, so CI enforces the gate
# on new code and the full `mutation` sweep stays a deliberate local run.
mutation-scope base="origin/main":
    #!/usr/bin/env bash
    set -euo pipefail
    filters="$(uv run --locked python scripts/quality/mutation_scope.py --base "{{ base }}")"
    if [ -z "$filters" ]; then
        printf '%s\n' 'No package module changed; nothing to mutate.'
        exit 0
    fi
    printf 'Mutating:\n%s\n' "$filters"
    # One invocation: mutmut accepts several filters, so coverage and test
    # association are collected once for the whole scope. The workspace is kept
    # -- regenerating fourteen thousand mutants costs minutes -- and the gate is
    # told the scope so stale verdicts from other modules are ignored.
    # shellcheck disable=SC2086
    uv run --locked python scripts/quality/mutation_runner.py run --max-children "{{ MUTATION_CHILDREN }}" $filters
    scopes=()
    while read -r filter; do scopes+=(--scope "$filter"); done <<< "$filters"
    just mutation-gate "${scopes[@]}"

# Run one changed-module shard. `filters` is the shard's space-separated list
# of mutmut filters, one per changed function. CI fans these shards out as a
# deterministic matrix so every selected module is evaluated without one job
# outliving its hosted-runner budget.
mutation-module filters:
    #!/usr/bin/env bash
    set -euo pipefail
    just mutation-clean
    read -r -a shard <<< "{{ filters }}"
    uv run --locked python scripts/quality/mutation_runner.py run --max-children "{{ MUTATION_CHILDREN }}" "${shard[@]}"
    scopes=()
    for filter in "${shard[@]}"; do scopes+=(--scope "$filter"); done
    just mutation-gate "${scopes[@]}"

# Fail on any unverified mutant the baseline does not already record, so a
# long-standing backlog stays visible without blocking unrelated work.
mutation-gate *scopes:
    #!/usr/bin/env bash
    set -uo pipefail
    results="${TMPDIR:-/tmp}/osm-polygon-website-tag-mutmut-results.txt"
    uv run --locked mutmut results --all true > "$results"
    uv run --locked python scripts/quality/mutation_gate.py \
        --results "$results" --baseline docs/quality/mutation-baseline.txt {{ scopes }}

smoke:
    just docker-smoke

quality: crap mutation

diff-review:
    git diff --check
    printf 'Staged changes:\n'
    git diff --cached --name-only -- .

qa-gauntlet: baseline ruff typecheck unit acceptance architecture crap mutation smoke diff-review

# Seconds, not minutes: only the tests a change can plausibly break.
# Tier 1: focused tests for the current diff.
focused base="origin/main":
    #!/usr/bin/env bash
    set -euo pipefail
    targets="$(uv run --locked python scripts/quality/select_tests.py --base "{{ base }}")"
    if [ -z "$targets" ]; then
        printf '%s\n' 'No testable change; nothing to run.'
        exit 0
    fi
    if [ "$targets" = "BROAD" ]; then
        printf '%s\n' 'Configuration changed; running the structural tests only.'
        uv run --locked pytest tests/architecture tests/quality -q
        exit 0
    fi
    printf 'Selected:\n%s\n' "$targets"
    # shellcheck disable=SC2086
    uv run --locked pytest $targets -q

# Bounded by construction -- it runs the selection above, never the whole
# suite. The pull request is what proves the whole suite.
# Tier 2: pre-push gate.
qa-push base="origin/main": ruff typecheck
    just focused "{{ base }}"

# `coverage` collects unit, acceptance and architecture in one instrumented
# run, and `crap` reads that run's artifact. The per-module mutation matrix
# runs beside this job; `mutation_scope.py` emits one shard per changed
# function.
# Tier 3: the pull-request gate.
qa-pr: baseline ruff typecheck coverage crap

# Everything the pull request proved, plus the container smoke test. In CI the
# Docker workflow owns the container gate and runs it beside the quality job,
# so this recipe is for proving a merge locally in one command.
# Tier 4: the merge gate.
qa-merge: qa-pr
    just smoke

# Never selected away and never weakened: recomputes the card, verifies every
# shard, and proves the plan without uploading.
# Tier 4: the strict data-integrity gate for a real run directory.
release-verify run_dir:
    uv run --locked osm-polygon-website-tag verify-results --run-dir "{{ run_dir }}"
    just release-stats-dry-run "{{ run_dir }}"

# Deprecated alias for `qa-pr`, kept so existing invocations keep working.
qa-ci: qa-pr

install-hooks:
    uv run --locked pre-commit install --hook-type pre-commit --hook-type pre-push

docker-image := "osm-polygon-website-tag:local"

docker-build:
    docker build --pull --target runtime --tag "{{docker-image}}" .

docker-smoke: docker-build
    docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m "{{docker-image}}" --help
