#!/usr/bin/env python3
"""Prepare the pipeline area's mutmut metadata for a safe same-commit resume."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path
from typing import Final, cast

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


def _read_metadata(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _has_expected_fields(metadata: dict[object, object]) -> bool:
    keys = metadata.keys()
    return keys >= _REQUIRED_META_FIELDS and keys <= _META_FIELDS


def _as_mapping(value: object) -> dict[object, object] | None:
    if isinstance(value, dict):
        return value
    return None


def _valid_exit_codes(values: dict[object, object]) -> bool:
    return all(
        isinstance(key, str) and (type(code) is int or code is None) for key, code in values.items()
    )


def _valid_hashes(values: dict[object, object]) -> bool:
    return all(isinstance(key, str) and isinstance(value, str) for key, value in values.items())


def _valid_type_errors(values: dict[object, object]) -> bool:
    return all(
        isinstance(key, str) and (isinstance(value, str) or value is None)
        for key, value in values.items()
    )


def _valid_metadata_document(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    if not _has_expected_fields(metadata):
        return False
    mappings = {name: _as_mapping(metadata.get(name, {})) for name in _META_FIELDS}
    if None in mappings.values():
        return False
    valid_mappings = cast(dict[str, dict[object, object]], mappings)
    return all(
        (
            _valid_exit_codes(valid_mappings["exit_code_by_key"]),
            _valid_hashes(valid_mappings["hash_by_function_name"]),
            _valid_type_errors(valid_mappings["type_check_error_by_key"]),
            _is_duration_mapping(valid_mappings["durations_by_key"]),
            _is_duration_mapping(valid_mappings["estimated_durations_by_key"]),
        )
    )


def _valid_metadata(path: Path) -> bool:
    """Check the mutmut 3.8 per-file state shape before reusing a checkpoint."""
    return _valid_metadata_document(_read_metadata(path))


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


def _read_checkpoint_status(marker: Path) -> str:
    try:
        return marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _clear_pipeline_metadata(root: Path) -> None:
    for path in _metadata_paths(root):
        path.unlink(missing_ok=True)


def _restore_valid_metadata(root: Path, checkpoint_dir: Path) -> None:
    for path in _metadata_paths(checkpoint_dir):
        if not _valid_metadata(path):
            continue
        relative_path = path.relative_to(checkpoint_dir)
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def prepare_checkpoint(root: Path) -> None:
    """Keep valid verdicts after a failed sweep; reset them after success or ambiguity.

    The workflow caches this staging directory only. It never restores generated
    source, bytecode, tests, or mutmut's test-association cache. Mutmut regenerates
    mutants from the current checkout and keeps verdicts only for unchanged names.
    """
    checkpoint_dir = root / _CHECKPOINT_DIR
    status = _read_checkpoint_status(checkpoint_dir / _CHECKPOINT_STATUS)
    _clear_pipeline_metadata(root)

    if status == "partial":
        _restore_valid_metadata(root, checkpoint_dir)

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


def _read_valid_exit_codes(path: Path) -> dict[str, int | None]:
    metadata = _read_metadata(path)
    if not _valid_metadata_document(metadata):
        raise RuntimeError(f"invalid pipeline mutation metadata: {path}")
    return cast(dict[str, int | None], cast(dict[str, object], metadata)["exit_code_by_key"])


def _verdict_summary(paths: tuple[Path, ...]) -> tuple[int, list[str]]:
    mutant_count = 0
    unchecked: list[str] = []
    for path in paths:
        exit_codes = _read_valid_exit_codes(path)
        mutant_count += len(exit_codes)
        unchecked.extend(name for name, code in exit_codes.items() if code is None)
    return mutant_count, unchecked


def verify_checkpoint(root: Path) -> None:
    """Fail unless every pipeline mutant has a recorded verdict."""
    metadata_paths = _metadata_paths(root)
    if not metadata_paths:
        raise RuntimeError("no pipeline mutants were generated")

    mutant_count, unchecked = _verdict_summary(metadata_paths)
    if mutant_count == 0:
        raise RuntimeError("no pipeline mutants were generated")
    if unchecked:
        sample = ", ".join(sorted(unchecked)[:5])
        raise RuntimeError(f"pipeline mutants were not checked ({len(unchecked)}): {sample}")


def _argument_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for checkpoint operations."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--root", type=Path, default=Path.cwd())
    mark = commands.add_parser("mark")
    mark.add_argument("--outcome", required=True)
    mark.add_argument("--root", type=Path, default=Path.cwd())
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def _run_verify(root: Path, parser: argparse.ArgumentParser) -> None:
    try:
        verify_checkpoint(root)
    except RuntimeError as exc:
        parser.exit(1, f"error: {exc}\n")


def _dispatch_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "prepare":
        prepare_checkpoint(args.root)
    elif args.command == "mark":
        mark_checkpoint(args.outcome, args.root)
    else:
        _run_verify(args.root, parser)


def main() -> None:
    """Prepare a checkpoint or mark the outcome of one pipeline-area sweep."""
    parser = _argument_parser()
    _dispatch_command(parser.parse_args(), parser)


if __name__ == "__main__":
    main()
