"""Artifact-derived statistics the dataset card renders."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def _write_shard(path: Path, rows: int) -> Path:
    """Write a Parquet shard whose footer records ``rows`` rows."""
    table = pa.table({"value": pa.array(range(rows), type=pa.int64())})
    pq.write_table(table, path)
    return path


def test_counting_shards_sums_every_footer(tmp_path: Path) -> None:
    paths = [_write_shard(tmp_path / f"s{index}.parquet", index + 1) for index in range(5)]

    assert card_stats._count_parquets(paths) == 1 + 2 + 3 + 4 + 5


def test_counting_no_shards_is_zero() -> None:
    assert card_stats._count_parquets([]) == 0


def test_counting_shards_consumes_a_one_shot_iterable(tmp_path: Path) -> None:
    """The count must not depend on the argument being re-iterable."""
    paths = iter([_write_shard(tmp_path / "only.parquet", 7)])

    assert card_stats._count_parquets(paths) == 7


def test_one_shard_row_count_reads_the_footer(tmp_path: Path) -> None:
    shard = _write_shard(tmp_path / "one.parquet", 3)

    assert card_stats._parquet_row_count(shard) == 3


def test_footers_are_read_through_a_bounded_pool(tmp_path: Path, monkeypatch) -> None:
    """Pin the pool width: an unbounded pool would still return the right sum.

    Footer reads are latency bound, so the worker count is the whole point of
    reading them concurrently; without this the width is unobservable.
    """
    observed: list[int | None] = []
    real_pool = card_stats.ThreadPoolExecutor

    def recording_pool(max_workers: int | None = None) -> ThreadPoolExecutor:
        observed.append(max_workers)
        return real_pool(max_workers=max_workers)

    monkeypatch.setattr(card_stats, "ThreadPoolExecutor", recording_pool)
    paths = [_write_shard(tmp_path / f"p{index}.parquet", 2) for index in range(3)]

    assert card_stats._count_parquets(paths) == 6
    assert observed == [card_stats._FOOTER_READ_WORKERS]
    assert card_stats._FOOTER_READ_WORKERS == 8


def test_an_empty_shard_list_never_starts_a_pool(monkeypatch) -> None:
    def refuse(max_workers: int | None = None) -> ThreadPoolExecutor:
        raise AssertionError("no shards means no pool")

    monkeypatch.setattr(card_stats, "ThreadPoolExecutor", refuse)

    assert card_stats._count_parquets([]) == 0
