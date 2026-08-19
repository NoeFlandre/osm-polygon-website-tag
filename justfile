set dotenv-load := false

default: check

sync:
    uv sync --locked

lock:
    uv lock --check

lint:
    uv run --locked ruff check .

format:
    uv run --locked ruff check . --fix
    uv run --locked ruff format .

format-check:
    uv run --locked ruff format --check .

typecheck:
    uv run --locked ty check src tests scripts

test:
    uv run --locked pytest

build:
    uv build

check: lock lint format-check typecheck test build

pre-commit:
    uv run --locked pre-commit run --all-files

pre-push:
    uv run --locked pre-commit run --all-files --hook-stage pre-push

coverage:
    uv run --locked pytest --cov=osm_polygon_website_tag --cov-report=term-missing --cov-report=json:/tmp/osm-polygon-website-tag-coverage.json --cov-fail-under=75

crap: coverage
    uv run --locked python scripts/quality/crap_report.py --coverage-json /tmp/osm-polygon-website-tag-coverage.json --path src/osm_polygon_website_tag/domain/tags.py --path src/osm_polygon_website_tag/contracts/polygon_schema.py --path src/osm_polygon_website_tag/application/workflow.py --max-crap 6

mutation:
    uv run --locked mutmut run --max-children 2
    uv run --locked mutmut results --all true | tee /tmp/osm-polygon-website-tag-mutmut-results.txt
    if rg -q ': (survived|timeout)' /tmp/osm-polygon-website-tag-mutmut-results.txt; then printf '%s\n' 'Mutation gate failed: surviving or timed-out mutants remain.' >&2; exit 1; fi

quality: crap mutation

install-hooks:
    uv run --locked pre-commit install --hook-type pre-commit --hook-type pre-push

docker-image := "osm-polygon-website-tag:local"

docker-build:
    docker build --pull --target runtime --tag "{{docker-image}}" .

docker-smoke: docker-build
    docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m "{{docker-image}}" --help
