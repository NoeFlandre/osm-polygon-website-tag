"""Tests for v1.4 language-field invariants."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.verification.language import verify_language_invariants


def _write_language_row(tmp_path: Path, *, probability: object) -> Path:
    path = tmp_path / "polygons" / "source.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "website": "https://example.org",
                    "website_text": "English text",
                    "website_text_status": "success",
                    "website_language": "eng_Latn",
                    "website_language_probability": probability,
                    "contact_website": None,
                    "contact_website_text": None,
                    "contact_website_text_status": "absent",
                    "contact_website_language": None,
                    "contact_website_language_probability": None,
                }
            ],
            schema=pa.schema(
                [
                    pa.field("website", pa.string()),
                    pa.field("website_text", pa.string()),
                    pa.field("website_text_status", pa.string()),
                    pa.field("website_language", pa.string()),
                    pa.field("website_language_probability", pa.float64()),
                    pa.field("contact_website", pa.string()),
                    pa.field("contact_website_text", pa.string()),
                    pa.field("contact_website_text_status", pa.string()),
                    pa.field("contact_website_language", pa.string()),
                    pa.field("contact_website_language_probability", pa.float64()),
                ]
            ),
        ),
        path,
    )
    return path


def test_verify_rejects_success_without_language_probability(tmp_path: Path) -> None:
    _write_language_row(tmp_path, probability=None)
    errors: list[str] = []

    verify_language_invariants(tmp_path, errors)

    assert any("language probability" in error for error in errors)


def test_verify_accepts_a_complete_language_pair(tmp_path: Path) -> None:
    _write_language_row(tmp_path, probability=0.91)
    errors: list[str] = []

    verify_language_invariants(tmp_path, errors)

    assert errors == []
