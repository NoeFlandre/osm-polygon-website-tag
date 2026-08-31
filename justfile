set dotenv-load := false
set export

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

mutation:
    uv run --locked python scripts/quality/mutation_runner.py run --max-children 2
    uv run --locked mutmut results --all true | tee /tmp/osm-polygon-website-tag-mutmut-results.txt
    if rg -q ': (survived|no tests|timeout|suspicious|segfault|check was interrupted)' /tmp/osm-polygon-website-tag-mutmut-results.txt; then printf '%s\n' 'Mutation gate failed: an unverified mutant remains.' >&2; exit 1; fi

smoke:
    just docker-smoke

quality: crap mutation

diff-review:
    git diff --check
    printf 'Staged changes:\n'
    git diff --cached --name-only -- .

qa-gauntlet: baseline ruff typecheck unit acceptance architecture crap mutation smoke diff-review

install-hooks:
    uv run --locked pre-commit install --hook-type pre-commit --hook-type pre-push

docker-image := "osm-polygon-website-tag:local"

docker-build:
    docker build --pull --target runtime --tag "{{docker-image}}" .

docker-smoke: docker-build
    docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m "{{docker-image}}" --help
