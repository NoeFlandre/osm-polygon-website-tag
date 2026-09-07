"""Artifact-derived statistics the dataset card renders."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.pipeline.analyze import SENTENCE_TABLE_SCHEMA
from osm_polygon_website_tag.reporting import card_stats
from osm_polygon_website_tag.reporting.card_stats import CardStats


def test_sentence_stats_come_from_the_analysis_table(tmp_path: Path) -> None:
    directory = tmp_path / "analysis"
    directory.mkdir()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"tag": "website", "status": "success", "row_count": 5, "sentence_count": 40},
                {
                    "tag": "website",
                    "status": "unsupported_language",
                    "row_count": 2,
                    "sentence_count": 0,
                },
                {
                    "tag": "contact_website",
                    "status": "success",
                    "row_count": 3,
                    "sentence_count": 12,
                },
                {"tag": "contact_website", "status": "absent", "row_count": 1, "sentence_count": 0},
            ],
            schema=SENTENCE_TABLE_SCHEMA,
        ),
        directory / "sentences.parquet",
    )
    stats = CardStats()

    card_stats._add_sentence_stats(stats, directory / "sentences.parquet")

    assert stats.website_sentence_row_count == 5
    assert stats.contact_website_sentence_row_count == 3
    assert stats.total_sentence_count == 52
    assert stats.unsupported_language_row_count == 2


def test_sentence_stats_are_absent_without_an_analysis_table(tmp_path: Path) -> None:
    stats = CardStats()

    card_stats._add_sentence_stats(stats, tmp_path / "missing.parquet")

    assert stats.total_sentence_count == 0
    assert stats.website_sentence_row_count == 0
