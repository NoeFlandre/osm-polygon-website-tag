"""Artifact-derived statistics the dataset card renders."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

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


def _text_batch(
    website: list[str | None],
    website_status: list[str],
    website_words: list[int | None],
    contact: list[str | None],
    contact_status: list[str],
    contact_words: list[int | None],
) -> pa.RecordBatch:
    return pa.record_batch(
        {
            "website": pa.array(website, type=pa.string()),
            "website_text_status": pa.array(website_status, type=pa.string()),
            "website_word_count": pa.array(website_words, type=pa.int64()),
            "contact_website": pa.array(contact, type=pa.string()),
            "contact_website_text_status": pa.array(contact_status, type=pa.string()),
            "contact_website_word_count": pa.array(contact_words, type=pa.int64()),
        }
    )


def test_a_text_batch_accumulates_each_field_from_its_own_column() -> None:
    """Both URL fields must stay wired to their own columns.

    Every total differs, so a batch read through the other field's column, or
    assigned rather than accumulated, cannot land on the same numbers.
    """
    stats = CardStats()
    batch = _text_batch(
        website=["https://a", None, "https://c", "https://d", None],
        website_status=["success", "absent", "empty", "empty", "pending"],
        website_words=[11, None, None, None, None],
        contact=["https://x", "https://y", None, "https://w", "https://v"],
        contact_status=["success", "success", "absent", "empty", "boom"],
        contact_words=[20, 5, None, None, None],
    )

    retryable = card_stats._add_text_batch(stats, batch)

    assert stats.website_urls_present == 3
    assert stats.contact_website_urls_present == 4
    assert stats.website_text_success_count == 1
    assert stats.contact_website_text_success_count == 2
    assert stats.website_text_empty_count == 2
    assert stats.contact_website_text_empty_count == 1
    # Every count is non-zero on at least one side, so a column replaced by
    # null cannot coincide with the expected totals.
    assert stats.website_text_failure_count == 0
    assert stats.contact_website_text_failure_count == 1
    assert stats.website_total_words == 11
    assert stats.contact_website_total_words == 25
    # `empty` and `pending` are not terminal, so this batch is still retryable.
    assert retryable is True


def test_text_batches_add_to_the_running_totals() -> None:
    stats = CardStats()
    batch = _text_batch(
        website=["https://a"],
        website_status=["success"],
        website_words=[4],
        contact=["https://x"],
        contact_status=["success"],
        contact_words=[7],
    )

    for _ in range(2):
        card_stats._add_text_batch(stats, batch)

    assert stats.website_urls_present == 2
    assert stats.contact_website_urls_present == 2
    assert stats.website_total_words == 8
    assert stats.contact_website_total_words == 14


def test_a_batch_is_retryable_when_either_status_is_nonterminal() -> None:
    """`absent` and `success` are terminal; anything else means unfinished."""
    only_website = _text_batch(["u"], ["pending"], [None], ["v"], ["success"], [1])
    only_contact = _text_batch(["u"], ["success"], [1], ["v"], ["pending"], [None])
    neither = _text_batch(["u"], ["success"], [1], ["v"], ["absent"], [None])

    assert card_stats._add_text_batch(CardStats(), only_website) is True
    assert card_stats._add_text_batch(CardStats(), only_contact) is True
    assert card_stats._add_text_batch(CardStats(), neither) is False


def test_only_the_exact_success_status_contributes_words() -> None:
    stats = CardStats()
    batch = _text_batch(["u"], ["SUCCESS"], [9], ["v"], ["Success"], [9])

    card_stats._add_text_batch(stats, batch)

    assert stats.website_total_words == 0
    assert stats.contact_website_total_words == 0


def test_url_counts_add_each_field_separately() -> None:
    stats = CardStats()
    stats.website_urls_present = 40
    stats.contact_website_urls_present = 70
    website = pa.array(["a", None, "c"], type=pa.string())
    contact = pa.array(["x", None, None], type=pa.string())

    card_stats._add_url_counts(stats, website, contact)
    card_stats._add_url_counts(stats, website, contact)

    assert stats.website_urls_present == 40 + 2 + 2
    assert stats.contact_website_urls_present == 70 + 1 + 1


def _cells(path: Path, rows: list[dict[str, object]], *, with_counts: bool = True) -> Path:
    columns: dict[str, pa.Array] = {
        "cell": pa.array([row["cell"] for row in rows], type=pa.string()),
        "level": pa.array([row["level"] for row in rows], type=pa.string()),
    }
    if with_counts:
        columns["row_count"] = pa.array([row["row_count"] for row in rows], type=pa.int64())
    pq.write_table(pa.table(columns), path)
    return path


def test_cell_stats_split_observation_from_canonical(tmp_path: Path) -> None:
    stats = CardStats()
    path = _cells(
        tmp_path / "cells.parquet",
        [
            {"cell": "a", "level": "observation", "row_count": 3},
            {"cell": "b", "level": "canonical", "row_count": 5},
            {"cell": "c", "level": "canonical", "row_count": 7},
        ],
    )

    card_stats._add_cell_stats(stats, path)

    assert stats.eight_cell_observation == {"a": 3}
    assert stats.eight_cell_canonical == {"b": 5, "c": 7}
    assert stats.canonical_count == 12


def test_only_the_exact_observation_level_is_an_observation(tmp_path: Path) -> None:
    """`level` and `observation` are stored enum values, matched exactly."""
    stats = CardStats()
    path = _cells(
        tmp_path / "cells.parquet",
        [
            {"cell": "a", "level": "OBSERVATION", "row_count": 1},
            {"cell": "b", "level": "Observation", "row_count": 2},
        ],
    )

    card_stats._add_cell_stats(stats, path)

    assert stats.eight_cell_observation == {}
    assert stats.eight_cell_canonical == {"a": 1, "b": 2}


def test_cell_stats_default_a_missing_row_count_to_zero(tmp_path: Path) -> None:
    """The reader tolerates a shard without the column rather than crashing."""
    stats = CardStats()
    path = _cells(
        tmp_path / "cells.parquet",
        [{"cell": "a", "level": "observation"}, {"cell": "b", "level": "canonical"}],
        with_counts=False,
    )

    card_stats._add_cell_stats(stats, path)

    assert stats.eight_cell_observation == {"a": 0}
    assert stats.eight_cell_canonical == {"b": 0}
    assert stats.canonical_count == 0


def test_cell_stats_ignore_a_missing_file(tmp_path: Path) -> None:
    stats = CardStats()

    card_stats._add_cell_stats(stats, tmp_path / "absent.parquet")

    assert stats.eight_cell_observation == {}
    assert stats.canonical_count == 0


class _FakeTextParquet:
    """Stand in for a shard: record how it is read and yield scripted batches."""

    opened: ClassVar[list[object]] = []
    reads: ClassVar[list[list[str]]] = []

    def __init__(self, shard: object, names: list[str], batches: list[object]) -> None:
        self.opened.append(shard)
        self.schema_arrow = pa.schema([(name, pa.string()) for name in names])
        self._batches = batches

    def iter_batches(self, *, columns: list[str], batch_size: int) -> list[object]:
        self.reads.append(columns)
        return self._batches


def _fake_text_shard(monkeypatch, retryable: list[bool], *, names=None) -> list[object]:
    _FakeTextParquet.opened = []
    _FakeTextParquet.reads = []
    batches = [object() for _ in retryable]
    verdicts = dict(zip(map(id, batches), retryable, strict=True))
    seen: list[object] = []
    columns = sorted(card_stats._TEXT_STATS_COLUMNS) if names is None else names
    monkeypatch.setattr(
        card_stats.pq,
        "ParquetFile",
        lambda shard: _FakeTextParquet(shard, [*columns, "extra"], batches),
    )

    def add_batch(_stats: CardStats, batch: object) -> bool:
        seen.append(batch)
        return verdicts[id(batch)]

    monkeypatch.setattr(card_stats, "_add_text_batch", add_batch)
    return seen


@pytest.mark.parametrize(
    ("retryable", "expected"),
    [
        ([], 6),
        ([False, False], 6),
        ([True, False], 5),
        ([False, True], 5),
        ([True, True], 5),
    ],
)
def test_text_stats_count_a_source_as_enriched_only_without_retryable_batches(
    monkeypatch, retryable: list[bool], expected: int
) -> None:
    seen = _fake_text_shard(monkeypatch, retryable)
    stats = CardStats(enriched_sources_count=5)
    shard = Path("shard.parquet")

    card_stats._add_text_stats(stats, shard)

    assert stats.enriched_sources_count == expected
    assert len(seen) == len(retryable)
    assert _FakeTextParquet.opened == [shard]
    assert _FakeTextParquet.reads == [sorted(card_stats._TEXT_STATS_COLUMNS)]


def test_text_stats_skip_a_shard_without_the_text_columns(monkeypatch) -> None:
    seen = _fake_text_shard(monkeypatch, [False], names=["website"])
    stats = CardStats(enriched_sources_count=5)

    card_stats._add_text_stats(stats, Path("shard.parquet"))

    assert stats.enriched_sources_count == 5
    assert seen == []
    assert _FakeTextParquet.reads == []


def _stub_card_pipeline(monkeypatch, analysis_dir: Path) -> list[tuple[str, tuple, dict]]:
    calls: list[tuple[str, tuple, dict]] = []

    def record(name: str, result: object = None):
        def stub(*args: object, **kwargs: object) -> object:
            calls.append((name, args, kwargs))
            return result

        return stub

    monkeypatch.setattr(card_stats, "_read_snapshot_status", record("snapshot", "complete"))
    monkeypatch.setattr(card_stats, "_set_density_stats", record("density"))
    monkeypatch.setattr(card_stats, "_artifact_paths", record("paths", ([], [], [], analysis_dir)))
    monkeypatch.setattr(card_stats, "_set_shard_counts", record("counts"))
    monkeypatch.setattr(card_stats, "_expected_source_count", record("expected", 0))
    monkeypatch.setattr(card_stats, "_add_public_shard_stats", record("public"))
    monkeypatch.setattr(card_stats, "compute_text_population_summary", record("population", "pop"))
    monkeypatch.setattr(card_stats, "_set_text_population_stats", record("text"))
    monkeypatch.setattr(card_stats, "_add_analysis_stats", record("analysis"))
    return calls


def _named(calls: list[tuple[str, tuple, dict]], name: str) -> list[tuple[tuple, dict]]:
    return [(args, kwargs) for called, args, kwargs in calls if called == name]


def test_card_stats_scope_text_and_density_to_the_selected_sources(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _stub_card_pipeline(monkeypatch, tmp_path)
    summary = object()

    card_stats.compute_card_stats(tmp_path, summary=summary, source_names=["a"])  # type: ignore

    [(_args, density)] = _named(calls, "density")
    assert density == {"summary": summary, "source_names": ["a"]}
    assert _named(calls, "population") == [((tmp_path,), {"source_names": ["a"]})]
    assert _named(calls, "analysis") == []


def test_card_stats_hand_the_computed_population_to_the_analysis_tables(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _stub_card_pipeline(monkeypatch, tmp_path)

    stats = card_stats.compute_card_stats(tmp_path)

    assert _named(calls, "population") == [((tmp_path,), {"source_names": None})]
    assert _named(calls, "analysis") == [((stats, tmp_path), {"text_population": "pop"})]


def test_card_stats_skip_a_missing_analysis_directory(tmp_path: Path, monkeypatch) -> None:
    calls = _stub_card_pipeline(monkeypatch, tmp_path / "absent")

    card_stats.compute_card_stats(tmp_path, text_population="given")  # type: ignore

    assert _named(calls, "population") == []
    assert _named(calls, "analysis") == []
    assert _named(calls, "text")[0][0][1] == "given"


def _stub_analysis_tables(monkeypatch) -> list[tuple[str, Path]]:
    calls: list[tuple[str, Path]] = []
    counts = {"duplicate_observations.parquet": 3, "conflicting_snapshots.parquet": 4}
    monkeypatch.setattr(card_stats, "_optional_row_count", lambda path: counts.get(path.name, -1))
    monkeypatch.setattr(card_stats, "_add_cell_stats", lambda _s, p: calls.append(("cells", p)))
    monkeypatch.setattr(card_stats, "_add_hostname_stats", lambda _s, p: calls.append(("hosts", p)))
    monkeypatch.setattr(
        card_stats, "_add_language_stats", lambda _s, p: calls.append(("languages", p))
    )
    monkeypatch.setattr(
        card_stats, "_add_sentence_stats", lambda _s, p: calls.append(("sentences", p))
    )
    return calls


def test_analysis_stats_read_every_table_by_its_exact_name(tmp_path: Path, monkeypatch) -> None:
    calls = _stub_analysis_tables(monkeypatch)
    stats = CardStats()

    card_stats._add_analysis_stats(stats, tmp_path)

    assert (stats.duplicate_count, stats.conflicting_snapshot_count) == (3, 4)
    assert calls == [
        ("cells", tmp_path / "cells_global.parquet"),
        ("hosts", tmp_path),
        ("languages", tmp_path / "languages.parquet"),
        ("sentences", tmp_path / "sentences.parquet"),
    ]
    assert [path.name for _name, path in calls] == [
        "cells_global.parquet",
        tmp_path.name,
        "languages.parquet",
        "sentences.parquet",
    ]


def test_analysis_stats_leave_languages_to_a_given_text_population(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _stub_analysis_tables(monkeypatch)

    card_stats._add_analysis_stats(CardStats(), tmp_path, text_population=object())  # type: ignore

    assert [name for name, _path in calls] == ["cells", "hosts", "sentences"]


def test_density_stats_compute_only_the_extracted_text_population(
    tmp_path: Path, monkeypatch
) -> None:
    requests: list[tuple[tuple, dict]] = []
    density = type(
        "Density", (), {"h3_resolution": 5, "occupied_cell_count": 6, "polygon_row_count": 7}
    )()
    monkeypatch.setattr(
        card_stats,
        "compute_polygon_density_summary",
        lambda *args, **kwargs: requests.append((args, kwargs)) or density,
    )
    stats = CardStats()

    card_stats._set_density_stats(stats, tmp_path, summary=None, source_names=["a"])

    assert requests == [((tmp_path,), {"source_names": ["a"], "extracted_text_only": True})]
    assert requests[0][1]["extracted_text_only"] is True
    assert (
        stats.polygon_density_h3_resolution,
        stats.occupied_h3_cell_count,
        stats.polygon_density_row_count,
    ) == (5, 6, 7)


def test_public_shard_stats_list_each_source_with_its_row_count(monkeypatch) -> None:
    monkeypatch.setattr(card_stats, "_add_enriched_source_count", lambda _stats, _shard: None)
    monkeypatch.setattr(card_stats, "_parquet_row_count", lambda shard: len(shard.stem))
    stats = CardStats()

    card_stats._add_public_shard_stats(stats, [Path("x/ab.parquet"), Path("x/cde.parquet")])

    assert stats.per_source_counts == [
        {"source_pbf": "ab.osm.pbf", "row_count": 2},
        {"source_pbf": "cde.osm.pbf", "row_count": 3},
    ]


class _SchemaParquet:
    """A shard exposing a schema and recording the columns each read asks for."""

    reads: ClassVar[list[list[str]]] = []

    def __init__(self, names: list[str]) -> None:
        self.schema_arrow = pa.schema([(name, pa.string()) for name in names])

    def iter_batches(self, *, columns: list[str], batch_size: int) -> list[object]:
        self.reads.append(columns)
        return [object()]


def test_enriched_source_count_adds_one_to_the_running_total(monkeypatch) -> None:
    names = sorted(card_stats._TEXT_STATS_COLUMNS)
    monkeypatch.setattr(card_stats.pq, "ParquetFile", lambda _shard: _SchemaParquet(names))
    monkeypatch.setattr(card_stats, "_has_retryable_text_status", lambda _parquet: False)
    stats = CardStats(enriched_sources_count=5)

    card_stats._add_enriched_source_count(stats, Path("a.parquet"))

    assert stats.enriched_sources_count == 6


def test_retryable_status_reads_only_the_two_status_columns(monkeypatch) -> None:
    _SchemaParquet.reads = []
    monkeypatch.setattr(card_stats, "status_has_retryable_value", lambda _column: False)
    parquet = _SchemaParquet([])
    parquet.iter_batches = lambda *, columns, batch_size: _SchemaParquet.reads.append(columns) or []  # type: ignore

    assert card_stats._has_retryable_text_status(parquet) is False  # type: ignore
    assert _SchemaParquet.reads == [["website_text_status", "contact_website_text_status"]]


def test_hostname_stats_load_both_tables(tmp_path: Path, monkeypatch) -> None:
    for name in ("top_hostnames_website.parquet", "top_hostnames_contact_website.parquet"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(
        card_stats.pq,
        "read_table",
        lambda path: pa.table({"name": [Path(path).name]}),
    )
    stats = CardStats()

    card_stats._add_hostname_stats(stats, tmp_path)

    assert stats.top_hostnames_website == [{"name": "top_hostnames_website.parquet"}]
    assert stats.top_hostnames_contact_website == [
        {"name": "top_hostnames_contact_website.parquet"}
    ]


def test_language_stats_total_each_tag_and_rank_by_count_then_name(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "languages.parquet"
    path.write_bytes(b"")
    rows = [
        {"tag": "website", "row_count": 2},
        {"tag": "website", "row_count": 5},
        {"tag": "contact_website", "row_count": 3},
    ]
    monkeypatch.setattr(card_stats.pq, "read_table", lambda _path: pa.Table.from_pylist(rows))
    monkeypatch.setattr(
        card_stats, "_combined_language_counts", lambda _rows: {"b": 2, "a": 2, "c": 5, "d": 1}
    )
    stats = CardStats()

    card_stats._add_language_stats(stats, path)

    assert stats.website_language_count == 7
    assert stats.contact_website_language_count == 3
    assert stats.detected_language_count == 4
    assert stats.top_languages == [("c", 5), ("a", 2), ("b", 2), ("d", 1)]


def test_language_sort_key_breaks_count_ties_by_name() -> None:
    assert sorted([("b", 2), ("a", 2)], key=card_stats._language_sort_key) == [("a", 2), ("b", 2)]


def test_artifact_paths_name_the_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=rf"^missing {tmp_path / 'polygons'}$"):
        card_stats._artifact_paths(tmp_path, source_names=None)


def test_optional_row_count_of_a_missing_table_is_zero(tmp_path: Path) -> None:
    assert card_stats._optional_row_count(tmp_path / "absent.parquet") == 0


def test_text_population_stats_copy_the_detected_language_count() -> None:
    stats = CardStats()

    card_stats._set_text_population_stats(
        stats, card_stats.TextPopulationSummary(detected_language_count=9)
    )

    assert stats.detected_language_count == 9


def test_unique_polygon_text_count_reads_shards_with_every_column(monkeypatch) -> None:
    names = [
        "osm_type",
        "osm_id",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    ]
    monkeypatch.setattr(card_stats.pq, "ParquetFile", lambda _shard: _SchemaParquet(names))
    monkeypatch.setattr(card_stats, "_text_polygon_ids", lambda _batch: {("way", 1)})
    stats = CardStats()

    card_stats._set_unique_polygon_text_count(stats, [Path("a.parquet")])

    assert stats.polygons_with_any_text == 1


def test_text_polygon_ids_include_contact_only_text() -> None:
    batch = pa.RecordBatch.from_pylist(
        [
            {
                "osm_type": "way",
                "osm_id": 1,
                "website_text": "hi",
                "website_text_status": "success",
                "contact_website_text": None,
                "contact_website_text_status": "absent",
            },
            {
                "osm_type": "node",
                "osm_id": 2,
                "website_text": None,
                "website_text_status": "absent",
                "contact_website_text": "yo",
                "contact_website_text_status": "success",
            },
        ]
    )

    assert card_stats._text_polygon_ids(batch) == {("way", 1), ("node", 2)}


def test_unsupported_language_counts_sum_each_language() -> None:
    unsupported = card_stats.SENTENCE_UNSUPPORTED_LANGUAGE
    rows = [
        {"status": unsupported, "language": "y", "row_count": 4},
        {"status": unsupported, "language": "x", "row_count": 1},
        {"status": unsupported, "language": "x", "row_count": 3},
        {"status": "other", "language": "x", "row_count": 9},
    ]

    assert card_stats._unsupported_language_counts(rows) == [("x", 4), ("y", 4)]
