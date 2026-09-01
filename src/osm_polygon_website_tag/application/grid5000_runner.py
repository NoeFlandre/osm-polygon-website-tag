"""Dependency-light entry point for one offline Grid'5000 bundle."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from osm_polygon_website_tag.pipeline.grid5000 import run_language_bundle


def _parser() -> argparse.ArgumentParser:
    """Build the small argument parser needed on a reserved compute node."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--time-budget-seconds", type=float)
    parser.add_argument("--batch-rows", type=int)
    parser.add_argument("--job-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one staged bundle and print its receipt as JSON."""
    args = _parser().parse_args(argv)
    try:
        result = run_language_bundle(
            args.bundle_dir,
            time_budget_seconds=args.time_budget_seconds,
            batch_rows=args.batch_rows,
            job_id=args.job_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.payload(), default=str, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
