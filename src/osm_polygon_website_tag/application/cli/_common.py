"""Global flags, shared option declarations, and output helpers for the CLI.

The package `__init__` owns the Typer app and the root callback. The command
modules import from here, never from the package, so there is no import cycle.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from osm_polygon_website_tag.runtime.config import Settings

DEBUG_ENV = "OSM_PWT_DEBUG"

RunDir = Annotated[Path, typer.Option("--run-dir", help="Existing run directory.")]

RepoId = Annotated[
    str | None,
    typer.Option(
        "--repo-id", help="Hugging Face dataset repository (defaults to HF_DATASET_REPO)."
    ),
]


@dataclass
class GlobalOptions:
    """Root-command flags. They apply to one invocation only."""

    debug: bool = False
    quiet: bool = False

    def reset(self) -> None:
        self.debug = False
        self.quiet = False


GLOBAL_OPTIONS = GlobalOptions()


def debug_requested() -> bool:
    return GLOBAL_OPTIONS.debug or os.environ.get(DEBUG_ENV) == "1"


def configured_hf_dataset_repo(repo_id: str | None) -> str:
    """Resolve an explicit CLI override or the current environment/.env setting."""
    return repo_id if repo_id is not None else Settings().hf_dataset_repo


def echo_json(payload: Any, *, sort_keys: bool = False) -> None:
    typer.echo(json.dumps(payload, default=str, indent=2, sort_keys=sort_keys))
