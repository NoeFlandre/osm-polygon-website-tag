"""Structural contracts for the runtime image, read from the files themselves."""

from __future__ import annotations

import fnmatch
from pathlib import Path

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
