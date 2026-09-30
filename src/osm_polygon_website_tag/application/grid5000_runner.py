"""Dependency-light entry point for one offline Grid'5000 bundle."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from osm_polygon_website_tag.application.grid5000_runner_base import (
    base_runner_parser,
    print_receipt,
)
from osm_polygon_website_tag.pipeline.grid5000 import run_language_bundle


def _parser() -> argparse.ArgumentParser:
    """Build the small argument parser needed on a reserved compute node."""
    return base_runner_parser(__doc__)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one staged bundle and print its receipt as JSON."""
    args = _parser().parse_args(argv)
    return print_receipt(
        lambda: run_language_bundle(
            args.bundle_dir,
            time_budget_seconds=args.time_budget_seconds,
            batch_rows=args.batch_rows,
            job_id=args.job_id,
        ).payload()
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
