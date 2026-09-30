"""Shared argument parser and receipt printing for the Grid'5000 node runners."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path


def base_runner_parser(description: str | None) -> argparse.ArgumentParser:
    """Build the flags every reserved-node bundle runner accepts."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--time-budget-seconds", type=float)
    parser.add_argument("--batch-rows", type=int)
    parser.add_argument("--job-id")
    return parser


def print_receipt(run: Callable[[], Mapping[str, object]]) -> int:
    """Run one bundle, print its receipt as JSON, and map ValueError to exit 2."""
    try:
        payload = run()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, default=str, indent=2, sort_keys=True))
    return 0
