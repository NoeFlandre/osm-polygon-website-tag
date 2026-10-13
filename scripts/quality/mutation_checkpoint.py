#!/usr/bin/env python3
"""Prepare the pipeline area's mutmut metadata for a safe same-commit resume."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path
from typing import Final

_CHECKPOINT_DIR: Final = Path(".mutation-checkpoint")
_CHECKPOINT_STATUS: Final = Path("status")
_PIPELINE_MUTANTS: Final = Path("mutants/src/osm_polygon_website_tag/pipeline")
_REQUIRED_META_FIELDS: Final = frozenset(
    {"exit_code_by_key", "durations_by_key", "estimated_durations_by_key"}
)
_OPTIONAL_META_FIELDS: Final = frozenset({"hash_by_function_name", "type_check_error_by_key"})
_META_FIELDS: Final = _REQUIRED_META_FIELDS | _OPTIONAL_META_FIELDS


def _metadata_paths(root: Path) -> tuple[Path, ...]:
    """Return only per-file mutant verdicts for the pipeline area."""
    directory = root / _PIPELINE_MUTANTS
    if not directory.is_dir():
        return ()
    return tuple(sorted(directory.rglob("*.py.meta")))


def _valid_metadata(path: Path) -> bool:
    """Check the mutmut 3.8 per-file state shape before reusing a checkpoint."""
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False

    if not isinstance(metadata, dict):
        return False
    if not metadata.keys() >= _REQUIRED_META_FIELDS or metadata.keys() - _META_FIELDS:
        return False
    mappings = {name: metadata.get(name, {}) for name in _META_FIELDS}
    if not all(isinstance(value, dict) for value in mappings.values()):
        return False

    exit_codes = metadata["exit_code_by_key"]
    hashes = mappings["hash_by_function_name"]
    type_errors = mappings["type_check_error_by_key"]
    durations = mappings["durations_by_key"]
    estimates = mappings["estimated_durations_by_key"]
    return (
        all(isinstance(key, str) for key in exit_codes)
        and all(type(code) is int or code is None for code in exit_codes.values())
        and all(isinstance(key, str) and isinstance(value, str) for key, value in hashes.items())
        and all(
            isinstance(key, str) and (isinstance(value, str) or value is None)
            for key, value in type_errors.items()
        )
        and _is_duration_mapping(durations)
        and _is_duration_mapping(estimates)
    )


def _is_duration_mapping(values: dict[object, object]) -> bool:
    """Require finite numeric durations and string mutant keys."""
    return all(isinstance(key, str) and _is_duration(value) for key, value in values.items())


def _is_duration(value: object) -> bool:
    """Return whether a JSON value is a finite numeric duration."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def prepare_checkpoint(root: Path) -> None:
    """Keep valid verdicts after a failed sweep; reset them after success or ambiguity.

    The workflow caches this staging directory only. It never restores generated
    source, bytecode, tests, or mutmut's test-association cache. Mutmut regenerates
    mutants from the current checkout and keeps verdicts only for unchanged names.
    """
    checkpoint_dir = root / _CHECKPOINT_DIR
    marker = checkpoint_dir / _CHECKPOINT_STATUS
    try:
        status = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        status = ""

    for path in _metadata_paths(root):
        path.unlink(missing_ok=True)

    metadata_paths = _metadata_paths(checkpoint_dir)
    if status == "partial":
        for path in metadata_paths:
            if _valid_metadata(path):
                relative_path = path.relative_to(checkpoint_dir)
                target = root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)

    shutil.rmtree(checkpoint_dir, ignore_errors=True)


def mark_checkpoint(outcome: str, root: Path) -> None:
    """Snapshot pipeline verdicts with a status for the next same-commit run."""
    status = "complete" if outcome == "success" else "partial"
    checkpoint_dir = root / _CHECKPOINT_DIR
    shutil.rmtree(checkpoint_dir, ignore_errors=True)
    marker = checkpoint_dir / _CHECKPOINT_STATUS
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{status}\n", encoding="utf-8")

    if status == "partial":
        for path in _metadata_paths(root):
            if not _valid_metadata(path):
                continue
            relative_path = path.relative_to(root)
            target = checkpoint_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def main() -> None:
    """Prepare a checkpoint or mark the outcome of one pipeline-area sweep."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--root", type=Path, default=Path.cwd())
    mark = commands.add_parser("mark")
    mark.add_argument("--outcome", required=True)
    mark.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if args.command == "prepare":
        prepare_checkpoint(args.root)
    else:
        mark_checkpoint(args.outcome, args.root)


if __name__ == "__main__":
    main()
