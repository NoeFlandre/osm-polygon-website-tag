"""Transactional, resumable polygon-shard text enrichment."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.enrich as enrich_module
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
)
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields
from osm_polygon_website_tag.pipeline.enrich import (
    DEFAULT_FETCH_WORKERS,
    MAX_FETCH_WORKERS,
    enrich_polygon_shard,
)
from osm_polygon_website_tag.web.text_extract import TextExtraction
from osm_polygon_website_tag.web.web_fetch import FetchResult


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _legacy_row(
    *,
    polygon_id: str = "source:way/1",
    website: str | None = "https://example.org",
    contact: str | None = "https://contact.example.org",
) -> dict[str, object]:
    return {
        "polygon_id": polygon_id,
        "region": "source",
        "source_pbf": "source.osm.pbf",
        "osm_type": "way",
        "osm_id": int(polygon_id.rsplit("/", 1)[1]),
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "name": None,
        "website": website,
        "contact_website": contact,
        "has_website": website is not None,
        "has_contact_website": contact is not None,
        "has_any_website": True,
        "website_class": "absolute_url" if website else None,
        "contact_website_class": "absolute_url" if contact else None,
        "website_hostname": "example.org" if website else None,
        "contact_website_hostname": "contact.example.org" if contact else None,
        "preferred_website": website or contact,
        "preferred_website_source": "website" if website else "contact:website",
        "wikidata": None,
        "wikidata_qid": None,
        "wikidata_class": None,
        "tags": json.dumps({"website": website, "contact:website": contact}),
        "tag_keys": '["contact:website","website"]',
        "tag_count": 2,
        "osm_primary_tag": "building",
        "geometry": '{"type":"Polygon","coordinates":[]}',
        "centroid": '{"type":"Point","coordinates":[0,0]}',
        "centroid_kind": "lambert_azimuthal_equal_area",
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0,0,0,0]",
        "area_m2": 1.0,
        "area_km2": 0.000001,
        "area_bucket": "<10m2",
        "schema_version": "v1.1",
    }


def _write_legacy(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA_V1_1), path)


def _current_row(index: int) -> dict[str, object]:
    row = _legacy_row(
        polygon_id=f"source:way/{index}",
        website="https://example.org",
        contact=None,
    )
    for field in (
        "preferred_website",
        "preferred_website_source",
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "area_km2",
    ):
        row.pop(field)
    row.update(initial_text_fields(website_present=True, contact_website_present=False))
    row["website_text"] = "text"
    row["website_word_count"] = 1
    row["website_text_status"] = "success"
    row["schema_version"] = "v1.3"
    return {field.name: row[field.name] for field in POLYGON_PUBLIC_SCHEMA}


def _extract(html: bytes, *, url: str) -> TextExtraction:
    text = html.decode()
    return TextExtraction("success", text, len(text.split()), None, "2.1.0")


def test_assemble_checkpoint_streams_arrow_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assembly writes Arrow batches without materializing every row in Python."""
    part = tmp_path / "parts" / "part-00000000.parquet"
    part.parent.mkdir()
    pq.write_table(
        pa.Table.from_pylist([_current_row(0), _current_row(1)], schema=POLYGON_PUBLIC_SCHEMA),
        part,
        compression="snappy",
    )

    def unexpected_row_sink(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("assembly must not construct BatchParquetSink")

    monkeypatch.setattr(enrich_module, "BatchParquetSink", unexpected_row_sink)
    staged = tmp_path / "staged.parquet"

    max_batch_rows = enrich_module._assemble_checkpoint(
        (part,),
        staged,
        batch_rows=2,
        row_count=2,
    )

    assert max_batch_rows == 2
    assert pq.read_schema(staged).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert [row["polygon_id"] for row in pq.read_table(staged).to_pylist()] == [
        "source:way/0",
        "source:way/1",
    ]
    repeated = tmp_path / "repeated.parquet"
    enrich_module._assemble_checkpoint((part,), repeated, batch_rows=2, row_count=2)
    assert repeated.read_bytes() == staged.read_bytes()


def test_legacy_shard_migrates_both_tags_without_pbf_access(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(shard, [_legacy_row()])
    fetched: list[str] = []

    def fetch(url: str) -> FetchResult:
        fetched.append(url)
        return FetchResult("ok", url, final_url=url, body=f"text from {url}".encode())

    result = enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetch,
        extractor=_extract,
    )

    row = pq.read_table(shard).to_pylist()[0]
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert row["schema_version"] == "v1.3"
    assert row["website_text"] == "text from https://example.org"
    assert row["contact_website_text"] == "text from https://contact.example.org"
    assert row["website_word_count"] == 3
    assert row["contact_website_word_count"] == 3
    assert fetched == ["https://example.org", "https://contact.example.org"]
    assert result.changed
    assert result.max_batch_rows == 1


def test_duplicate_url_across_both_tags_fetches_once(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(
        shard,
        [_legacy_row(website="https://example.org", contact="https://example.org")],
    )
    calls = 0

    def fetch(url: str) -> FetchResult:
        nonlocal calls
        calls += 1
        return FetchResult("ok", url, final_url=url, body=b"same full text")

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetch,
        extractor=_extract,
    )

    assert calls == 1


def test_unique_urls_are_fetched_concurrently_in_stable_row_order(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        _legacy_row(
            polygon_id=f"source:way/{index}", website=f"https://example.org/{index}", contact=None
        )
        for index in range(16)
    ]
    _write_legacy(shard, rows)
    lock = threading.Lock()
    active = 0
    peak = 0

    def fetch(url: str) -> FetchResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return FetchResult("ok", url, final_url=url, body=f"text from {url}".encode())

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetch,
        extractor=_extract,
    )

    output = pq.read_table(shard).to_pylist()
    assert peak >= 2
    assert peak <= DEFAULT_FETCH_WORKERS
    assert [row["website_text"] for row in output] == [
        f"text from https://example.org/{index}" for index in range(16)
    ]


def test_fetch_workers_is_configurable_and_bounded(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        _legacy_row(
            polygon_id=f"source:way/{index}", website=f"https://example.org/{index}", contact=None
        )
        for index in range(8)
    ]
    _write_legacy(shard, rows)
    lock = threading.Lock()
    active = 0
    peak = 0

    def fetch(url: str) -> FetchResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return FetchResult("ok", url, final_url=url, body=f"text from {url}".encode())

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetch,
        extractor=_extract,
        fetch_workers=2,
    )

    assert peak >= 2
    assert peak <= 2


def test_fetch_workers_rejects_values_outside_safe_bound(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=f"between 1 and {MAX_FETCH_WORKERS}"):
        enrich_polygon_shard(
            tmp_path / "missing.parquet",
            cache_path=tmp_path / "cache.sqlite3",
            invocation_id="one",
            fetch_workers=MAX_FETCH_WORKERS + 1,
        )


def test_interrupted_enrichment_keeps_completed_batches_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        _legacy_row(
            polygon_id=f"source:way/{index}", website=f"https://example.org/{index}", contact=None
        )
        for index in range(4)
    ]
    _write_legacy(shard, rows)
    monkeypatch.setattr("osm_polygon_website_tag.pipeline.enrich.DEFAULT_FETCH_WORKERS", 1)
    first_calls: list[str] = []

    def interrupting_fetch(url: str) -> FetchResult:
        first_calls.append(url)
        if url.endswith("/3"):
            raise KeyboardInterrupt
        return FetchResult("ok", url, final_url=url, body=f"text from {url}".encode())

    with pytest.raises(KeyboardInterrupt):
        enrich_polygon_shard(
            shard,
            cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
            invocation_id="one",
            fetcher=interrupting_fetch,
            extractor=_extract,
            batch_rows=2,
        )

    checkpoint_dir = shard.with_name(f".{shard.name}.enriching.parts")
    first_part = checkpoint_dir / "part-00000000.parquet"
    assert first_part.is_file()
    assert pq.ParquetFile(first_part).metadata.num_rows == 2
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA_V1_1, check_metadata=True)

    resumed_calls: list[str] = []

    def resuming_fetch(url: str) -> FetchResult:
        resumed_calls.append(url)
        return FetchResult("ok", url, final_url=url, body=f"text from {url}".encode())

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="two",
        fetcher=resuming_fetch,
        extractor=_extract,
        batch_rows=2,
    )

    assert first_calls[:2] == ["https://example.org/0", "https://example.org/1"]
    assert resumed_calls == ["https://example.org/3"]
    assert not checkpoint_dir.exists()
    output = pq.read_table(shard).to_pylist()
    assert [row["website_text"] for row in output] == [
        f"text from https://example.org/{index}" for index in range(4)
    ]


def test_failed_url_retries_on_next_invocation(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(shard, [_legacy_row(contact=None)])
    cache = tmp_path / "run" / "cache" / "text.sqlite3"

    enrich_polygon_shard(
        shard,
        cache_path=cache,
        invocation_id="one",
        fetcher=lambda url: FetchResult("fetch_error", url, message="TimeoutError"),
        extractor=_extract,
    )
    assert pq.read_table(shard)["website_text_status"][0].as_py() == "fetch_error"

    enrich_polygon_shard(
        shard,
        cache_path=cache,
        invocation_id="two",
        fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"recovered text"),
        extractor=_extract,
    )

    row = pq.read_table(shard).to_pylist()[0]
    assert row["website_text_status"] == "success"
    assert row["website_text"] == "recovered text"


def test_promotion_failure_preserves_prior_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(shard, [_legacy_row()])
    original = shard.read_bytes()

    def fail(_pairs):
        raise OSError("injected promotion failure")

    monkeypatch.setattr("osm_polygon_website_tag.pipeline.enrich.atomic_promote_bundle", fail)

    with pytest.raises(OSError, match="injected"):
        enrich_polygon_shard(
            shard,
            cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
            invocation_id="one",
            fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"text"),
            extractor=_extract,
        )

    assert shard.read_bytes() == original
