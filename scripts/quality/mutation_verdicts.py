#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fnmatch
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

_PIPELINE_MUTANTS: Final = Path("mutants/src/osm_polygon_website_tag/pipeline")
_PIPELINE_MODULE_PREFIX: Final = "osm_polygon_website_tag.pipeline."
_INTERRUPTED_EXIT_CODE: Final = 2
_REQUIRED_META_FIELDS: Final = frozenset(
    {"exit_code_by_key", "durations_by_key", "estimated_durations_by_key"}
)
_OPTIONAL_META_FIELDS: Final = frozenset({"hash_by_function_name", "type_check_error_by_key"})
_META_FIELDS: Final = _REQUIRED_META_FIELDS | _OPTIONAL_META_FIELDS


def _module_metadata_path(root: Path, module: str) -> Path:
    if not module.startswith(_PIPELINE_MODULE_PREFIX):
        raise ValueError(f"module must be inside the pipeline package: {module!r}")
    parts = module.split(".")
    if not all(part.isidentifier() for part in parts):
        raise ValueError(f"module is not a dotted Python name: {module!r}")
    source = root / _PIPELINE_MUTANTS.joinpath(*parts[2:])
    source = source / "__init__.py" if source.is_dir() else source.with_suffix(".py")
    return Path(f"{source}.meta")


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


def _is_duration(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_duration_mapping(values: dict[object, object]) -> bool:
    return all(isinstance(key, str) and _is_duration(value) for key, value in values.items())


def _valid_metadata_document(metadata: object) -> bool:
    if not isinstance(metadata, dict) or not _has_expected_fields(metadata):
        return False
    mappings = {name: _as_mapping(metadata.get(name, {})) for name in _META_FIELDS}
    if None in mappings.values():
        return False
    fields = cast(dict[str, dict[object, object]], mappings)
    return all(
        (
            _valid_exit_codes(fields["exit_code_by_key"]),
            _valid_hashes(fields["hash_by_function_name"]),
            _valid_type_errors(fields["type_check_error_by_key"]),
            _is_duration_mapping(fields["durations_by_key"]),
            _is_duration_mapping(fields["estimated_durations_by_key"]),
        )
    )


def _valid_metadata(path: Path) -> bool:
    return _valid_metadata_document(_read_metadata(path))


def _read_exit_codes(path: Path) -> dict[str, int | None]:
    metadata = _read_metadata(path)
    if not _valid_metadata_document(metadata):
        raise RuntimeError(f"invalid pipeline mutation metadata: {path}")
    return cast(dict[str, int | None], cast(dict[str, object], metadata)["exit_code_by_key"])


def _selected_exit_codes(
    exit_codes: dict[str, int | None], filters: Sequence[str]
) -> dict[str, int | None]:
    return {
        name: code
        for name, code in exit_codes.items()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in filters)
    }


def _validate_filters(module: str, filters: Sequence[str]) -> None:
    if not filters or any(
        not pattern.startswith(f"{module}.") or not pattern.endswith("__mutmut_*")
        for pattern in filters
    ):
        raise ValueError(f"filters must select mutants inside {module!r}")


def _selected_shard_exit_codes(
    root: Path, module: str, filters: Sequence[str]
) -> dict[str, int | None]:
    metadata_path = _module_metadata_path(root, module)
    if not metadata_path.is_file():
        raise RuntimeError(f"no pipeline mutants were generated for {module}")

    selected = _selected_exit_codes(_read_exit_codes(metadata_path), filters)
    if not selected:
        raise RuntimeError(f"no pipeline mutants were generated for {module}")
    return selected


def _reject_unfinished_mutants(module: str, selected: dict[str, int | None]) -> None:
    unfinished = [
        name for name, code in selected.items() if code is None or code == _INTERRUPTED_EXIT_CODE
    ]
    if unfinished:
        sample = ", ".join(sorted(unfinished)[:5])
        raise RuntimeError(
            f"pipeline mutants were interrupted or not checked in {module} "
            f"({len(unfinished)}): {sample}"
        )


def verify_shard(root: Path, module: str, filters: Sequence[str]) -> None:
    _validate_filters(module, filters)
    selected = _selected_shard_exit_codes(root, module, filters)
    _reject_unfinished_mutants(module, selected)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    verify = parser.add_subparsers(dest="command", required=True).add_parser("verify")
    verify.add_argument("--root", type=Path, default=Path.cwd())
    verify.add_argument("--module", required=True)
    verify.add_argument("--filters", nargs="+", required=True)
    return parser


def main() -> None:
    parser = _argument_parser()
    args = parser.parse_args()
    try:
        verify_shard(args.root, args.module, args.filters)
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
