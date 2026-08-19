"""Static contracts for the reproducible Docker development/runtime images."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_uses_locked_multi_stage_non_root_defaults() -> None:
    dockerfile = _read("Dockerfile")

    assert "FROM python:3.12-slim-bookworm@sha256:" in dockerfile
    assert "FROM ghcr.io/astral-sh/uv:0.11.16@sha256:" in dockerfile
    assert "COPY pyproject.toml uv.lock .python-version README.md LICENSE ./" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "uv sync --locked --group dev" in dockerfile
    assert "USER app" in dockerfile
    assert 'ENTRYPOINT ["osm-polygon-website-tag"]' in dockerfile
    assert 'CMD ["--help"]' in dockerfile
    assert "MPLBACKEND=Agg" in dockerfile


def test_dockerfile_installs_osmium_runtime_library() -> None:
    dockerfile = _read("Dockerfile")

    assert "apt-get install --no-install-recommends -y libexpat1" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile


def test_dockerignore_excludes_credentials_and_production_data() -> None:
    dockerignore = _read(".dockerignore")

    for pattern in (".env", ".env.*", "*.pbf", "*.osm", "*.parquet", "data/", "runs/"):
        assert pattern in dockerignore


def test_docker_smoke_workflow_builds_without_production_inputs() -> None:
    workflow = _read(".github/workflows/docker.yml")

    assert "docker build" in workflow
    assert "--target runtime" in workflow
    assert "--help" in workflow
    assert "HF_TOKEN" not in workflow
    assert "source-root" not in workflow
    assert "contents: read" in workflow


def test_setup_documents_docker_data_and_secret_boundaries() -> None:
    setup = _read("docs/setup.md")

    assert "Docker" in setup
    assert "--read-only" in setup
    assert "readonly" in setup
    assert "HF_TOKEN" in setup
    assert "run-all" in setup
