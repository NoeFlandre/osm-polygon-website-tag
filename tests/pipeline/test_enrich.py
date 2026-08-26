"""Transactional, resumable polygon-shard text enrichment."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import legacy_polygon_row, write_legacy_polygon_shard

import osm_polygon_website_tag.pipeline.enrichment_checkpoint as checkpoint_module
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields
from osm_polygon_website_tag.pipeline.enrich import (
    DEFAULT_FETCH_WORKERS,
    MAX_FETCH_WORKERS,
    _apply_cached_results,
    _apply_result,
    _completed_fetch,
    _drain_interrupted_fetches,
    _extract_fetched,
    _fetch,
    _finalize_batch,
    _has_complete_text,
    _mark_absent,
    _mark_invalid_url,
    _prepare_batch,
    _queue_tag,
    _record_fetched,
    _record_fetches,
    _record_one_fetch,
    _resolve_pending,
    _skip_checkpointed_rows,
    _submit_fetches,
    _validate_enrichment_settings,
    enrich_polygon_shard,
)
from osm_polygon_website_tag.web.text_cache import CachedText, TextCache
from osm_polygon_website_tag.web.text_extract import TextExtraction
from osm_polygon_website_tag.web.web_fetch import FetchResult


def _current_row(index: int) -> dict[str, object]:
    row = legacy_polygon_row(
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


def test_private_enrichment_state_helpers_are_deterministic(tmp_path: Path) -> None:
    _validate_enrichment_settings(1, 1)
    with pytest.raises(ValueError, match="fetch_workers"):
        _validate_enrichment_settings(0, 1)
    with pytest.raises(ValueError, match="batch_rows"):
        _validate_enrichment_settings(1, 0)
    rows: list[dict[str, object]] = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert _skip_checkpointed_rows(rows, 1) == ([{"id": 2}, {"id": 3}], 0)
    assert _skip_checkpointed_rows(rows, 5) == ([], 2)
    assert _skip_checkpointed_rows(rows, 0) == (rows, 0)

    row: dict[str, object] = {}
    _mark_absent(row, "website")
    assert row == {
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "absent",
    }
    assert not _has_complete_text(row, "website")
    _apply_result(
        row,
        "website",
        CachedText("https://example.org", "success", "text", 1, None, None, 0, "", None, "run"),
    )
    assert _has_complete_text(row, "website")
    _mark_invalid_url(row, "website", "ftp://example.org", "run")
    assert row["website_text_status"] == "invalid_url"


def test_private_enrichment_url_queue_and_fetch_helpers(tmp_path: Path) -> None:
    row: dict[str, object] = {
        "website": "https://example.org",
        "website_text_status": "pending",
    }
    pending: dict[str, list[tuple[dict[str, object], str]]] = {}
    lookup: set[str] = set()
    _queue_tag(
        row,
        value_column="website",
        field_prefix="website",
        invocation_id="run",
        pending=pending,
        lookup_urls=lookup,
    )
    assert lookup == {"https://example.org"}
    assert pending == {"https://example.org": [(row, "website")]}
    assert (
        _fetch("https://example.org", fetcher=lambda url: FetchResult("fetch_error", url)).status
        == "fetch_error"
    )
    fetched = FetchResult("ok", "https://example.org", final_url=None, body=b"hello world")
    cached = _extract_fetched(
        "https://example.org", fetched, invocation_id="run", extractor=_extract
    )
    assert cached.status == "success"
    assert cached.word_count == 2
    failed_future: Future[FetchResult] = Future()
    failed_future.set_exception(RuntimeError("worker failed"))
    assert _completed_fetch(failed_future) is None
    cache = TextCache(tmp_path / "cache.sqlite3")
    try:
        unresolved = _apply_cached_results(
            pending,
            lookup,
            cache=cache,
            invocation_id="run",
        )
        assert unresolved == pending
        _record_fetched(
            "https://example.org",
            fetched,
            pending["https://example.org"],
            cache=cache,
            invocation_id="run",
            extractor=_extract,
        )
        assert row["website_text"] == "hello world"
        cache.flush()
    finally:
        cache.close()


def test_private_enrichment_batch_and_future_helpers(tmp_path: Path) -> None:
    original = _current_row(1)
    original["website_text"] = None
    original["website_word_count"] = None
    original["website_text_status"] = "pending"
    states, pending, urls = _prepare_batch(
        [original],
        source_schema=POLYGON_PUBLIC_SCHEMA,
        invocation_id="run",
    )
    assert len(states) == 1
    assert urls == {"https://example.org"}
    assert pending["https://example.org"]
    assert _finalize_batch(states)[0]["schema_version"] == "v1.3"
    cache = TextCache(tmp_path / "cache.sqlite3")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = _submit_fetches(
                pending,
                fetch_pool=pool,
                fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"text"),
            )
            _record_fetches(
                pending,
                futures,
                cache=cache,
                invocation_id="run",
                extractor=_extract,
            )
            assert states[0].row["website_text"] == "text"
            direct_future: Future[FetchResult] = Future()
            direct_future.set_result(
                FetchResult(
                    "ok", "https://example.org", final_url="https://example.org", body=b"direct"
                )
            )
            _record_one_fetch(
                "https://example.org",
                direct_future,
                pending["https://example.org"],
                cache=cache,
                invocation_id="run-direct",
                extractor=_extract,
            )
            _resolve_pending(
                {},
                cache=cache,
                invocation_id="run",
                fetcher=lambda _url: pytest.fail("empty pending must not fetch"),
                extractor=_extract,
                fetch_pool=pool,
            )
            _drain_interrupted_fetches({}, {}, cache=cache, invocation_id="run", extractor=_extract)
    finally:
        cache.close()


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

    monkeypatch.setattr(checkpoint_module, "BatchParquetSink", unexpected_row_sink)
    staged = tmp_path / "staged.parquet"

    max_batch_rows = checkpoint_module.assemble_checkpoint(
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
    checkpoint_module.assemble_checkpoint((part,), repeated, batch_rows=2, row_count=2)
    assert repeated.read_bytes() == staged.read_bytes()


def test_legacy_shard_migrates_both_tags_without_pbf_access(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    write_legacy_polygon_shard(shard, [legacy_polygon_row()])
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
    assert len(fetched) == 2
    assert set(fetched) == {"https://example.org", "https://contact.example.org"}
    assert result.changed
    assert result.max_batch_rows == 1


def test_duplicate_url_across_both_tags_fetches_once(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    write_legacy_polygon_shard(
        shard,
        [legacy_polygon_row(website="https://example.org", contact="https://example.org")],
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


def test_enrichment_bulk_reads_each_batch_url_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        legacy_polygon_row(
            polygon_id=f"source:way/{index}",
            website=f"https://example.org/{index % 2}",
            contact=None,
        )
        for index in range(8)
    ]
    write_legacy_polygon_shard(shard, rows)
    cache_path = tmp_path / "run" / "cache" / "text.sqlite3"
    cache = TextCache(cache_path)
    for index in range(2):
        url = f"https://example.org/{index}"
        cache.record(
            CachedText(
                url,
                "success",
                f"cached text {index}",
                3,
                url,
                None,
                1,
                "2026-01-01T00:00:00+00:00",
                "2.1.0",
                "seed",
            ),
            invocation_id="seed",
        )
    cache.close()
    lookups: list[tuple[str, ...]] = []
    original_bulk_lookup = TextCache.get_reusable_many

    def bulk_lookup(
        self: TextCache,
        urls: set[str],
        *,
        invocation_id: str,
    ) -> dict[str, CachedText]:
        lookups.append(tuple(sorted(urls)))
        return original_bulk_lookup(self, urls, invocation_id=invocation_id)

    monkeypatch.setattr(TextCache, "get_reusable_many", bulk_lookup)

    enrich_polygon_shard(
        shard,
        cache_path=cache_path,
        invocation_id="run-1",
        fetcher=lambda _url: pytest.fail("cached URLs must not be fetched"),
        extractor=_extract,
    )

    assert lookups == [("https://example.org/0", "https://example.org/1")]


def test_unique_urls_are_fetched_concurrently_in_stable_row_order(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        legacy_polygon_row(
            polygon_id=f"source:way/{index}", website=f"https://example.org/{index}", contact=None
        )
        for index in range(16)
    ]
    write_legacy_polygon_shard(shard, rows)
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


def test_text_extraction_runs_on_caller_thread_for_native_parser_safety(tmp_path: Path) -> None:
    """Keep the lxml-backed extractor out of concurrent fetch worker threads."""
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        legacy_polygon_row(
            polygon_id=f"source:way/{index}",
            website=f"https://example.org/{index}",
            contact=None,
        )
        for index in range(4)
    ]
    write_legacy_polygon_shard(shard, rows)
    caller_thread = threading.get_ident()
    extractor_threads: set[int] = set()

    def extractor(html: bytes, *, url: str) -> TextExtraction:
        extractor_threads.add(threading.get_ident())
        return _extract(html, url=url)

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"text"),
        extractor=extractor,
        fetch_workers=2,
    )

    assert extractor_threads == {caller_thread}


def test_fetch_workers_is_configurable_and_bounded(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        legacy_polygon_row(
            polygon_id=f"source:way/{index}", website=f"https://example.org/{index}", contact=None
        )
        for index in range(8)
    ]
    write_legacy_polygon_shard(shard, rows)
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
        legacy_polygon_row(
            polygon_id=f"source:way/{index}", website=f"https://example.org/{index}", contact=None
        )
        for index in range(4)
    ]
    write_legacy_polygon_shard(shard, rows)
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
    write_legacy_polygon_shard(shard, [legacy_polygon_row(contact=None)])
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
    write_legacy_polygon_shard(shard, [legacy_polygon_row()])
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


def test_v1_4_enrichment_preserves_language_fields(tmp_path: Path) -> None:
    row = _current_row(1)
    row.update(
        {
            "website_text": None,
            "website_word_count": None,
            "website_text_status": "pending",
            "website_language": "eng_Latn",
            "website_language_probability": 0.93,
            "contact_website_language": None,
            "contact_website_language_probability": None,
        }
    )
    shard = tmp_path / "source.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA_V1_4), shard)

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "cache.sqlite3",
        invocation_id="run",
        fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"recovered text"),
        extractor=_extract,
    )

    result = pq.read_table(shard).to_pylist()[0]
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
    assert result["website_language"] == "eng_Latn"
    assert result["website_language_probability"] == 0.93
