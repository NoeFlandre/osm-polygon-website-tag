"""Structural contracts for the runtime image, read from the files themselves."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# Paths that must never enter the build context: credentials and production data.
_KEPT_OUT_OF_THE_IMAGE = (
    ".env",
    ".env.production",
    "region.pbf",
    "region.osm",
    "shard.parquet",
    "data/runs/run.json",
    "runs/run.json",
)


def _last_stage(dockerfile: str) -> list[list[str]]:
    """The instructions of the final build stage, each split into words."""
    stages: list[list[list[str]]] = []
    for line in dockerfile.splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        if words[0].upper() == "FROM":
            stages.append([])
        if stages:
            stages[-1].append(words)
    return stages[-1]


def _instruction(stage: list[list[str]], name: str) -> list[str]:
    """The arguments of the last ``name`` instruction in a stage."""
    matches = [words[1:] for words in stage if words[0].upper() == name]
    return matches[-1] if matches else []


def _ignored(path: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("/") + "/")
        for pattern in patterns
    )


def test_the_runtime_image_runs_as_a_non_root_user_with_an_entrypoint() -> None:
    stage = _last_stage((ROOT / "Dockerfile").read_text(encoding="utf-8"))

    user = _instruction(stage, "USER")

    assert user
    assert user[0] not in {"root", "0"}
    assert _instruction(stage, "ENTRYPOINT")


def test_the_build_context_keeps_credentials_and_data_out_of_the_image() -> None:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.startswith("#")]

    leaked = [path for path in _KEPT_OUT_OF_THE_IMAGE if not _ignored(path, patterns)]

    assert leaked == []


def test_the_runtime_image_defaults_its_data_root_to_the_mounted_volume() -> None:
    stage = _last_stage((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    environment = [word for words in stage if words[0].upper() == "ENV" for word in words[1:]]

    assert "OSM_POLY_DATA_DIR=/data" in environment


def test_the_runtime_image_prepares_a_writable_grid5000_bundle_directory() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "mkdir -p /data/raw /data/runs /data/models /data/grid5000" in dockerfile
    assert "chown app:app /data/runs /data/models /data/grid5000" in dockerfile


def _compose_service() -> Any:
    import yaml

    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    return compose["services"]["pipeline"]


def test_the_compose_service_is_read_only_non_root_and_mounts_raw_input_read_only() -> None:
    service = _compose_service()
    volumes = {volume["target"]: volume for volume in service["volumes"]}

    assert service["read_only"] is True
    assert service["build"]["target"] == "runtime"
    assert not str(service["user"]).startswith("0")
    assert volumes["/data/raw"]["read_only"] is True
    assert "read_only" not in volumes["/data/runs"]
    assert "/data/models" in volumes


def test_compose_uses_bash_safe_host_uid_and_gid_variables() -> None:
    service = _compose_service()
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    setup = (ROOT / "docs" / "setup.md").read_text(encoding="utf-8")

    assert service["user"] == "${HOST_UID:-10001}:${HOST_GID:-10001}"
    assert 'HOST_UID="$(id -u)" HOST_GID="$(id -g)"' in compose
    assert 'HOST_UID="$(id -u)" HOST_GID="$(id -g)"' in setup


def test_the_compose_service_mounts_a_writable_grid5000_bundle_directory() -> None:
    service = _compose_service()
    volumes = {volume["target"]: volume for volume in service["volumes"]}

    assert volumes["/data/grid5000"]["source"] == "${OSM_GRID5000_DIR:-./data/grid5000}"
    assert "read_only" not in volumes["/data/grid5000"]


def test_the_compose_service_carries_no_secret_and_reads_an_optional_env_file() -> None:
    service = _compose_service()

    assert service["env_file"] == [{"path": ".env", "required": False}]
    assert "HF_TOKEN" not in service["environment"]
