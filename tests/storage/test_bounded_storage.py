"""Executable bounded-storage contracts."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink
from osm_polygon_website_tag.storage.candidate_ledger import (
    DEFAULT_COMMIT_BATCH_SIZE,
    CandidateLedger,
    _candidate_payload,
)
from osm_polygon_website_tag.storage.duckdb_engine import (
    _make_connection,
    canonical_observations,
    cells_global_canonical,
    cells_global_observation,
    copy_query_atomic,
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


def test_candidate_payload_decodes_the_persisted_fields() -> None:
    assert _candidate_payload(
        '{"website":"https://example.org"}',
        3,
        "2024-01-01T00:00:00+00:00",
        "closed_way",
    ) == {
        "tags": {"website": "https://example.org"},
        "osm_version": 3,
        "osm_timestamp": _TS,
        "candidate_kind": "closed_way",
    }


def test_duckdb_private_helpers_configure_and_copy_atomically(tmp_path: Path) -> None:
    connection = _make_connection(tmp_path / "duckdb", memory_limit="64MB")
    try:
        connection.execute(
            "CREATE TABLE observations (osm_type VARCHAR, osm_id BIGINT, osm_version INTEGER, osm_timestamp TIMESTAMP, source_pbf VARCHAR, has_website BOOLEAN, has_contact_website BOOLEAN, has_wikidata BOOLEAN)"
        )
        connection.execute(
            "INSERT INTO observations VALUES ('way', 1, 1, TIMESTAMP '2024-01-01', 'a.osm.pbf', true, false, false)"
        )
        assert cells_global_observation(connection)[0]["cell_100_w1_c0_d0"] == 1
        canonical_observations(connection)
        assert cells_global_canonical(connection)[0]["cell_100_w1_c0_d0"] == 1
        destination = tmp_path / "result.parquet"
        copy_query_atomic(connection, "SELECT 1 AS value", destination)
        assert destination.exists()
        assert pq.read_table(destination).to_pylist() == [{"value": 1}]
    finally:
        connection.close()


@pytest.mark.parametrize("bad_batch_size", [0, -1, -4096])
def test_candidate_ledger_rejects_non_positive_batch_size(
    tmp_path: Path, bad_batch_size: int
) -> None:
    with pytest.raises(ValueError) as error:
        CandidateLedger(tmp_path / "candidates.sqlite3", commit_batch_size=bad_batch_size)
    assert str(error.value) == (
        f"commit_batch_size must be a positive integer, got {bad_batch_size!r}"
    )


def test_candidate_ledger_accepts_batch_size_one_and_creates_nested_parent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "ledger" / "candidates.sqlite3"
    ledger = CandidateLedger(path, commit_batch_size=1)

    assert path.parent.is_dir()
    assert ledger.path == path
    assert ledger._closed is False
    ledger.upsert("way", 1, {"website": "https://one.example"}, 1, _TS, "closed_way")

    assert _externally_committed_candidate_count(path) == 1
    ledger.close()


def test_upsert_persists_canonical_json_encoding(tmp_path: Path) -> None:
    path = tmp_path / "candidates.sqlite3"
    ledger = CandidateLedger(path, commit_batch_size=1)
    ledger.upsert(
        "way",
        1,
        {"z": "last", "a": "first"},
        1,
        _TS,
        "closed_way",
    )
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT tags_json FROM candidates").fetchone()
        assert row == ('{"a":"first","z":"last"}',)
    finally:
        connection.close()
        ledger.close()


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

    assert candidate == {
        "tags": {"website": "https://same-connection.example"},
        "osm_version": 1,
        "osm_timestamp": _TS,
        "candidate_kind": "closed_way",
    }
    ledger.close()


def test_mark_area_seen_preserves_sql_contract_and_rejects_duplicates(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path / "candidates.sqlite3", commit_batch_size=1)
    ledger.upsert("way", 1, {"website": "https://example.org"}, 1, _TS, "closed_way")
    statements: list[str] = []
    ledger._db.set_trace_callback(statements.append)

    assert ledger.mark_area_seen("way", 1)
    assert "SELECT area_seen FROM candidates WHERE osm_type='way' AND osm_id=1" in statements
    assert "UPDATE candidates SET area_seen=1 WHERE osm_type='way' AND osm_id=1" in statements
    with pytest.raises(ValueError, match="duplicate_area_callback"):
        ledger.mark_area_seen("way", 1)
    ledger.close()


def test_flush_only_commits_pending_mutations_and_resets_the_counter(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path / "candidates.sqlite3", commit_batch_size=100)
    statements: list[str] = []
    ledger._db.set_trace_callback(statements.append)

    ledger._flush()
    assert "COMMIT" not in statements

    ledger.upsert("way", 1, {"website": "https://example.org"}, 1, _TS, "closed_way")
    ledger._flush()

    assert "COMMIT" in statements
    assert ledger._pending_mutations == 0
    ledger.close()


def test_flush_does_not_commit_when_no_mutations_are_pending() -> None:
    ledger = object.__new__(CandidateLedger)
    connection = Mock(spec=sqlite3.Connection)
    ledger._db = cast(sqlite3.Connection, connection)
    ledger._pending_mutations = 0

    ledger._flush()

    connection.commit.assert_not_called()


def test_close_is_idempotent_and_marks_the_ledger_closed(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path / "candidates.sqlite3")

    ledger.close()
    ledger.close()

    assert ledger._closed is True


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
