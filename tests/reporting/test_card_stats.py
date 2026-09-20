"""Artifact-derived statistics the dataset card renders."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.pipeline.analyze import LANGUAGE_TABLE_SCHEMA, SENTENCE_TABLE_SCHEMA
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
                    "language": "hrv_Latn",
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
    assert stats.sentence_split_eligible_count == 10
    assert stats.sentence_split_supported_count == 8
    assert stats.sentence_split_unsupported_count == 2
    assert stats.top_unsupported_sentence_languages == [("hrv_Latn", 2)]


def test_sentence_stats_are_absent_without_an_analysis_table(tmp_path: Path) -> None:
    stats = CardStats()

    card_stats._add_sentence_stats(stats, tmp_path / "missing.parquet")

    assert stats.total_sentence_count == 0
    assert stats.website_sentence_row_count == 0


def test_language_stats_count_each_tag_and_sort_combined_labels(tmp_path: Path) -> None:
    path = tmp_path / "languages.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"tag": "website", "language": "eng_Latn", "row_count": 2},
                {"tag": "website", "language": "deu_Latn", "row_count": 1},
                {"tag": "contact_website", "language": "deu_Latn", "row_count": 3},
            ],
            schema=LANGUAGE_TABLE_SCHEMA,
        ),
        path,
    )
    stats = CardStats()

    card_stats._add_language_stats(stats, path)

    assert stats.website_language_count == 3
    assert stats.contact_website_language_count == 3
    assert stats.detected_language_count == 2
    assert stats.top_languages == [("deu_Latn", 4), ("eng_Latn", 2)]
