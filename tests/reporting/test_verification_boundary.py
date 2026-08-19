"""Architecture contracts for the internal verification implementation."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_verification_is_split_into_focused_modules() -> None:
    package = ROOT / "src/osm_polygon_website_tag/reporting/verification"

    assert (package / "__init__.py").is_file()
    for name in ("shards.py", "text.py", "rows.py", "analysis.py", "receipt.py"):
        assert (package / name).is_file()
