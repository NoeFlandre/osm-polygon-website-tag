"""Executable bounded-storage contracts."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.batch_sink import BatchParquetSink
from osm_polygon_website_tag.candidate_ledger import CandidateLedger


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
