"""Executable bounded-storage contracts."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink
from osm_polygon_website_tag.storage.candidate_ledger import (
    DEFAULT_COMMIT_BATCH_SIZE,
    CandidateLedger,
)

_TS = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def _externally_committed_candidate_count(path: Path) -> int:
    """Count candidates visible to a *separate* connection.

    A second connection can only read rows the writer has committed. While the
    ledger holds an open (uncommitted) transaction, SQLite lets a reader take a
    shared lock and observe the last committed snapshot, which excludes the
    pending mutations. Therefore a non-zero count here proves a batch boundary
    was committed; a zero count means the mutations are still uncommitted.
    """
    connection = sqlite3.connect(path, timeout=0.0)
    try:
        row = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()
        return int(row[0])
    finally:
        connection.close()


def test_parquet_sink_never_exceeds_configured_batch(tmp_path: Path) -> None:
    schema = pa.schema([pa.field("value", pa.int64(), nullable=False)])
    path = tmp_path / "bounded.parquet"
    sink = BatchParquetSink(path, schema, batch_rows=7)

    for value in range(10_000):
        sink.add({"value": value})
    sink.close()

    assert sink.max_pending_rows <= 7
    assert sink.row_count == 10_000
    assert pq.ParquetFile(path).metadata.num_rows == 10_000


def test_candidate_ledger_reconciles_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "candidates.sqlite3"
    ledger = CandidateLedger(path)
    timestamp = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    for osm_id in range(1_000):
        ledger.upsert(
            "way",
            osm_id,
            {"website": f"https://{osm_id}.example"},
            1,
            timestamp,
            "closed_way",
        )
    for osm_id in range(0, 1_000, 2):
        assert ledger.mark_area_seen("way", osm_id)

    missing_ids = [osm_id for _osm_type, osm_id, _row in ledger.missing_areas()]
    ledger.close()

    assert path.is_file()
    assert missing_ids == list(range(1, 1_000, 2))


def test_candidate_ledger_retry_discards_stale_temporary_database(tmp_path: Path) -> None:
    path = tmp_path / "candidates.sqlite3"
    first = CandidateLedger(path)
    first.upsert(
        "way",
        1,
        {"website": "https://stale.example"},
        1,
        dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        "closed_way",
    )
    first.close()

    retry = CandidateLedger(path)

    assert list(retry.missing_areas()) == []
    retry.close()


def test_default_commit_batch_size_is_bounded() -> None:
    assert DEFAULT_COMMIT_BATCH_SIZE == 4096


@pytest.mark.parametrize("bad_batch_size", [0, -1, -4096])
def test_candidate_ledger_rejects_non_positive_batch_size(
    tmp_path: Path, bad_batch_size: int
) -> None:
    with pytest.raises(ValueError):
        CandidateLedger(tmp_path / "candidates.sqlite3", commit_batch_size=bad_batch_size)


def test_mutations_visible_to_same_connection_before_commit(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path / "candidates.sqlite3", commit_batch_size=1_000)
    ledger.upsert(
        "way",
        1,
        {"website": "https://same-connection.example"},
        1,
        _TS,
        "closed_way",
    )
    assert ledger.mark_area_seen("way", 1)

    candidate = ledger.get("way", 1)

    assert candidate is not None
    assert candidate["tags"] == {"website": "https://same-connection.example"}
    ledger.close()


def test_commit_occurs_at_configured_threshold(tmp_path: Path) -> None:
    path = tmp_path / "candidates.sqlite3"
    ledger = CandidateLedger(path, commit_batch_size=3)
    ledger.upsert("way", 1, {"website": "https://1.example"}, 1, _TS, "closed_way")
    ledger.upsert("way", 2, {"website": "https://2.example"}, 1, _TS, "closed_way")

    # Below the threshold: mutations are uncommitted, so a separate reader
    # still observes the previous committed snapshot (zero candidates).
    assert _externally_committed_candidate_count(path) == 0

    # The third mutation crosses the threshold and commits the batch.
    ledger.upsert("way", 3, {"website": "https://3.example"}, 1, _TS, "closed_way")

    assert _externally_committed_candidate_count(path) == 3
    ledger.close()


def test_final_flush_on_close(tmp_path: Path) -> None:
    path = tmp_path / "candidates.sqlite3"
    ledger = CandidateLedger(path, commit_batch_size=1_000)
    for osm_id in range(1, 6):
        ledger.upsert(
            "way",
            osm_id,
            {"website": f"https://{osm_id}.example"},
            1,
            _TS,
            "closed_way",
        )

    # No batch boundary reached: mutations are uncommitted and externally
    # invisible.
    assert _externally_committed_candidate_count(path) == 0

    ledger.close()

    assert _externally_committed_candidate_count(path) == 5


def test_missing_areas_exact_after_batched_operations(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path / "candidates.sqlite3", commit_batch_size=4)
    for osm_id in range(1, 6):
        ledger.upsert(
            "way",
            osm_id,
            {"website": f"https://{osm_id}.example", "name": f"n{osm_id}"},
            osm_id,
            _TS,
            "closed_way",
        )
    assert ledger.mark_area_seen("way", 1)
    assert ledger.mark_area_seen("way", 2)
    assert ledger.mark_area_seen("way", 3)
    assert ledger.mark_area_seen("way", 4)

    missing = list(ledger.missing_areas())
    ledger.close()

    assert [(osm_type, osm_id) for osm_type, osm_id, _row in missing] == [("way", 5)]
    row = missing[0][2]
    assert row["tags"] == {"website": "https://5.example", "name": "n5"}
    assert row["osm_version"] == 5
    assert row["candidate_kind"] == "closed_way"
    assert row["osm_timestamp"] == _TS


def test_batched_ledger_retry_starts_from_clean_database(tmp_path: Path) -> None:
    path = tmp_path / "candidates.sqlite3"
    first = CandidateLedger(path, commit_batch_size=2)
    for osm_id in range(1, 4):
        first.upsert(
            "way",
            osm_id,
            {"website": f"https://{osm_id}.example"},
            1,
            _TS,
            "closed_way",
        )
    first.close()
    assert path.is_file()

    retry = CandidateLedger(path, commit_batch_size=2)

    assert list(retry.missing_areas()) == []
    retry.close()
