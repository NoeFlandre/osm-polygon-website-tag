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
    uv run --locked pytest

unit:
    uv run --locked pytest tests --ignore=tests/acceptance --ignore=tests/architecture

acceptance:
    uv run --locked pytest tests/acceptance

architecture:
    uv run --locked pytest tests/architecture

build:
    uv build --out-dir "{{ BUILD_OUTPUT_DIR }}"

check: lock lint format-check typecheck test build

pre-commit:
    uv run --locked pre-commit run --all-files

pre-push:
    uv run --locked pre-commit run --all-files --hook-stage pre-push

coverage:
    uv run --locked pytest --cov=osm_polygon_website_tag --cov-report=term-missing --cov-report=json:/tmp/osm-polygon-website-tag-coverage.json --cov-fail-under=75

crap: coverage
    uv run --locked python scripts/quality/crap_report.py --coverage-json /tmp/osm-polygon-website-tag-coverage.json --path src/osm_polygon_website_tag --max-crap 6

mutation: mutation-clean
    uv run --locked python scripts/quality/mutation_runner.py run --max-children "{{ MUTATION_CHILDREN }}"
    just mutation-gate

# Results persist in the mutant workspace between invocations, so a run must
# start from a clean one or the gate reads verdicts for mutants it never
# checked.
mutation-clean:
    rm -rf mutants

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

# The gates CI runs: identical to `qa-gauntlet` except that mutation is scoped
# to the modules the change touches, so the job finishes inside a hosted
# runner's lifetime instead of being reclaimed mid-sweep.
qa-ci base="origin/main": baseline ruff typecheck unit acceptance architecture crap
    just mutation-scope "{{ base }}"
    just smoke

install-hooks:
    uv run --locked pre-commit install --hook-type pre-commit --hook-type pre-push

docker-image := "osm-polygon-website-tag:local"

docker-build:
    docker build --pull --target runtime --tag "{{docker-image}}" .

docker-smoke: docker-build
    docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m "{{docker-image}}" --help
