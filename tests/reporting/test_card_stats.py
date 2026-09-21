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


def _status(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


def _mask(values: list[bool]) -> pa.Array:
    return pa.array(values, type=pa.bool_())


def test_status_counts_accumulate_each_field_independently() -> None:
    """Every counter must add to what is there and read its own column.

    The six increments are deliberately distinct so a counter that is assigned
    instead of accumulated, subtracted, or wired to the other URL field cannot
    produce the same totals.
    """
    stats = CardStats()
    stats.website_text_success_count = 100
    stats.contact_website_text_success_count = 200
    stats.website_text_empty_count = 300
    stats.contact_website_text_empty_count = 400
    stats.website_text_failure_count = 500
    stats.contact_website_text_failure_count = 600

    website_status = _status(["success", "empty", "empty", "boom", "pending"])
    contact_status = _status(["empty", "boom", "boom", "boom", "success"])
    website_success = _mask([True, False, False, False, False])
    contact_success = _mask([True, True, False, False, False])

    card_stats._add_status_counts(
        stats, website_status, contact_status, website_success, contact_success
    )

    assert stats.website_text_success_count == 100 + 1
    assert stats.contact_website_text_success_count == 200 + 2
    assert stats.website_text_empty_count == 300 + 2
    assert stats.contact_website_text_empty_count == 400 + 1
    assert stats.website_text_failure_count == 500 + 1
    assert stats.contact_website_text_failure_count == 600 + 3


def test_status_counts_add_again_on_a_second_batch() -> None:
    """A shard is read in batches, so the second call must keep the first."""
    stats = CardStats()
    website_status = _status(["empty", "boom"])
    contact_status = _status(["boom", "boom"])
    website_success = _mask([True, False])
    contact_success = _mask([False, False])

    for _ in range(2):
        card_stats._add_status_counts(
            stats, website_status, contact_status, website_success, contact_success
        )

    assert stats.website_text_success_count == 2
    assert stats.contact_website_text_success_count == 0
    assert stats.website_text_empty_count == 2
    assert stats.contact_website_text_empty_count == 0
    assert stats.website_text_failure_count == 2
    assert stats.contact_website_text_failure_count == 4


def test_only_the_exact_lowercase_empty_status_counts_as_empty() -> None:
    """`empty` is a stored enum value, not a case-insensitive label."""
    stats = CardStats()
    statuses = _status(["EMPTY", "Empty", "empty"])
    no_success = _mask([False, False, False])

    card_stats._add_status_counts(stats, statuses, statuses, no_success, no_success)

    assert stats.website_text_empty_count == 1
    assert stats.contact_website_text_empty_count == 1
    # The two mis-cased values are unknown statuses, so they are failures.
    assert stats.website_text_failure_count == 2


def test_invalid_statuses_count_everything_outside_the_known_set() -> None:
    known = _status(["absent", "pending", "success", "empty"])

    assert card_stats._count_invalid_statuses(known) == 0


def test_invalid_statuses_count_nulls_and_unknown_values() -> None:
    statuses = _status(["absent", None, "boom", "PENDING", "Success", "EMPTY", ""])

    assert card_stats._count_invalid_statuses(statuses) == 6


def test_each_known_status_is_recognised_exactly() -> None:
    """One assertion per accepted value, so dropping any one is visible."""
    for accepted in ("absent", "pending", "success", "empty"):
        assert card_stats._count_invalid_statuses(_status([accepted])) == 0, accepted
