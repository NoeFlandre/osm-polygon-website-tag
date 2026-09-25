#!/usr/bin/env python3
"""Snapshot the language codes the installed wtpsplit segmenter supports.

wtpsplit lives in the optional ``sentences`` extra, so the default suite cannot
import it. This script records ``Constants.LANGINFO.index`` -- the first column
of the package's ``language_info.csv`` -- into a checked-in fixture that the
suite asserts against unconditionally. Re-run it whenever the wtpsplit pin
changes:

    uv run --locked --extra sentences python scripts/quality/wtpsplit_languages.py
"""

from __future__ import annotations

import argparse
import csv
import io
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "wtpsplit_languages.txt"


def render_snapshot(version: str, language_info_csv: str) -> str:
    """Return the fixture text: a version header, then one sorted code per line."""
    rows = csv.reader(io.StringIO(language_info_csv))
    next(rows)
    codes = sorted({row[0] for row in rows if row and row[0]})
    return "\n".join([f"# wtpsplit=={version}", *codes]) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=FIXTURE)
    args = parser.parse_args(argv)

    distribution = metadata.distribution("wtpsplit")
    source = Path(str(distribution.locate_file("wtpsplit/data/language_info.csv")))
    snapshot = render_snapshot(distribution.version, source.read_text(encoding="utf-8"))
    args.output.write_text(snapshot, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
