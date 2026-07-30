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
    uv run --locked ty check src tests

test:
    uv run --locked pytest

build:
    uv build

check: lock lint format-check typecheck test build

pre-commit:
    uv run --locked pre-commit run --all-files

pre-push:
    uv run --locked pre-commit run --all-files --hook-stage pre-push

install-hooks:
    uv run --locked pre-commit install --hook-type pre-commit --hook-type pre-push
