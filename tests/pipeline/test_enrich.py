"""Transactional, resumable polygon-shard text enrichment."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import legacy_polygon_row, write_legacy_polygon_shard

import osm_polygon_website_tag.pipeline.checkpoint_storage as checkpoint_storage
import osm_polygon_website_tag.pipeline.enrich as enrich_module
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
    _dispatch_extraction,
    _drain_interrupted_fetches,
    _extract_default_fetch,
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
from osm_polygon_website_tag.pipeline.enrichment_checkpoint import enrichment_checkpoint_store
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


class ConcurrentFetchRecorder:
    """Fetch fixture that measures concurrent calls and returns stable bodies."""

    def __init__(self, *, delay_seconds: float = 0.03) -> None:
        self.delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def __call__(self, url: str) -> FetchResult:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        time.sleep(self.delay_seconds)
        with self._lock:
            self._active -= 1
        return FetchResult("ok", url, final_url=url, body=f"text from {url}".encode())


class RecordingTextCache:
    """Record normalized cache entries at the enrichment boundary."""

    def __init__(self) -> None:
        self.records: list[tuple[CachedText, str]] = []
        self.flush_count = 0

    def record(self, value: CachedText, *, invocation_id: str) -> CachedText:
        self.records.append((value, invocation_id))
        return value

    def flush(self) -> None:
        self.flush_count += 1


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


def test_queue_tag_marks_absent_invalid_and_complete_values_without_fetching() -> None:
    pending: dict[str, list[tuple[dict[str, object], str]]] = {}
    lookup: set[str] = set()

    absent: dict[str, object] = {"website": None, "contact_website_text": "kept"}
    _queue_tag(
        absent,
        value_column="website",
        field_prefix="website",
        invocation_id="run",
        pending=pending,
        lookup_urls=lookup,
    )
    assert absent == {
        "website": None,
        "contact_website_text": "kept",
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "absent",
    }

    invalid: dict[str, object] = {"website": "ftp://example.org", "website_text": "stale"}
    _queue_tag(
        invalid,
        value_column="website",
        field_prefix="website",
        invocation_id="run",
        pending=pending,
        lookup_urls=lookup,
    )
    assert invalid == {
        "website": "ftp://example.org",
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "invalid_url",
    }

    complete: dict[str, object] = {
        "website": "https://example.org",
        "website_text": "cached text",
        "website_word_count": 2,
        "website_text_status": "success",
    }
    _queue_tag(
        complete,
        value_column="website",
        field_prefix="website",
        invocation_id="run",
        pending=pending,
        lookup_urls=lookup,
    )

    assert complete["website_text"] == "cached text"
    assert lookup == set()
    assert pending == {}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"website_text_status": "success", "website_text": "text", "website_word_count": 1}, True),
        (
            {"website_text_status": "fetch_error", "website_text": "text", "website_word_count": 1},
            False,
        ),
        ({"website_text_status": "success", "website_text": None, "website_word_count": 1}, False),
        (
            {"website_text_status": "success", "website_text": "text", "website_word_count": None},
            False,
        ),
    ],
)
def test_complete_text_requires_success_text_and_word_count(
    row: dict[str, object], expected: bool
) -> None:
    assert _has_complete_text(row, "website") is expected


def test_mark_invalid_url_clears_previous_text_fields() -> None:
    row: dict[str, object] = {
        "website_text": "stale text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website_text": "separate field",
    }

    _mark_invalid_url(row, "website", "ftp://example.org", "run-7")

    assert row == {
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "invalid_url",
        "contact_website_text": "separate field",
    }


def test_record_fetches_records_in_source_order_and_updates_all_references() -> None:
    first_url = "https://example.org/first"
    second_url = "https://example.org/second"
    first_row: dict[str, object] = {}
    second_rows: tuple[dict[str, object], dict[str, object]] = ({}, {})
    pending = {
        first_url: [(first_row, "website")],
        second_url: [
            (second_rows[0], "website"),
            (second_rows[1], "contact_website"),
        ],
    }
    futures: dict[str, Future[FetchResult]] = {url: Future() for url in pending}
    futures[second_url].set_result(
        FetchResult("ok", second_url, final_url=second_url, body=b"second page")
    )
    futures[first_url].set_result(FetchResult("fetch_error", first_url, message="http_503"))
    cache = RecordingTextCache()

    _record_fetches(
        pending,
        futures,
        cache=cast(TextCache, cache),
        invocation_id="run",
        extractor=_extract,
    )

    assert [value.url for value, _invocation in cache.records] == [first_url, second_url]
    assert all(invocation == "run" for _value, invocation in cache.records)
    assert first_row == {
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "fetch_error",
    }
    assert second_rows == (
        {
            "website_text": "second page",
            "website_word_count": 2,
            "website_text_status": "success",
        },
        {
            "contact_website_text": "second page",
            "contact_website_word_count": 2,
            "contact_website_text_status": "success",
        },
    )


def test_resolve_pending_fetches_a_miss_and_records_its_cache_value() -> None:
    url = "https://example.org/page"
    row: dict[str, object] = {}
    pending = {url: [(row, "website"), (row, "contact_website")]}
    fetched_urls: list[str] = []
    cache = RecordingTextCache()

    def fetch(value: str) -> FetchResult:
        fetched_urls.append(value)
        return FetchResult("ok", value, final_url=value, body=b"page text")

    with ThreadPoolExecutor(max_workers=1) as pool:
        _resolve_pending(
            pending,
            cache=cast(TextCache, cache),
            invocation_id="resolve-run",
            fetcher=fetch,
            extractor=_extract,
            fetch_pool=pool,
        )

    assert fetched_urls == [url]
    assert [(value.url, value.status, value.text) for value, _ in cache.records] == [
        (url, "success", "page text")
    ]
    assert cache.records[0][1] == "resolve-run"
    assert row == {
        "website_text": "page text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website_text": "page text",
        "contact_website_word_count": 2,
        "contact_website_text_status": "success",
    }


def test_record_one_fetch_preserves_failure_metadata_for_all_references() -> None:
    url = "https://example.org/page"
    first_row: dict[str, object] = {}
    second_row: dict[str, object] = {}
    future: Future[FetchResult] = Future()
    future.set_result(
        FetchResult(
            "fetch_error",
            url,
            final_url="https://example.org/blocked",
            message="http_429",
        )
    )
    cache = RecordingTextCache()

    _record_one_fetch(
        url,
        future,
        [(first_row, "website"), (second_row, "contact_website")],
        cache=cast(TextCache, cache),
        invocation_id="retry-2",
        extractor=_extract,
    )

    assert cache.records == [
        (
            CachedText(
                url,
                "fetch_error",
                None,
                None,
                "https://example.org/blocked",
                "http_429",
                0,
                "",
                None,
                "retry-2",
            ),
            "retry-2",
        )
    ]
    assert first_row == {
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "fetch_error",
    }
    assert second_row == {
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "fetch_error",
    }


def test_dispatch_extraction_defers_only_successful_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.org/page"
    successful = FetchResult("ok", url, final_url=url, body=b"page")
    expected = CachedText(url, "success", "page", 1, url, None, 0, "", "2.1.0", "run")
    submitted: list[tuple[object, ...]] = []

    class ExtractPool:
        def submit(self, function: object, *args: object) -> Future[CachedText]:
            submitted.append((function, *args))
            future: Future[CachedText] = Future()
            future.set_result(expected)
            return future

    extracted: dict[str, CachedText] = {}
    deferred: dict[str, Future[CachedText]] = {}
    pool = ExtractPool()
    _dispatch_extraction(
        url,
        successful,
        invocation_id="run",
        extractor=_extract,
        extract_pool=cast(ProcessPoolExecutor, pool),
        extracted=extracted,
        deferred_extractions=deferred,
    )
    assert extracted == {}
    assert deferred[url].result() == expected
    assert submitted == [(_extract_default_fetch, url, successful, "run")]

    failed = FetchResult("fetch_error", url, message="http_503")
    failure = CachedText(url, "fetch_error", None, None, None, "http_503", 0, "", None, "run")
    monkeypatch.setattr(enrich_module, "_extract_fetched", lambda *_args, **_kwargs: failure)
    _dispatch_extraction(
        url,
        failed,
        invocation_id="run",
        extractor=_extract,
        extract_pool=cast(ProcessPoolExecutor, pool),
        extracted=extracted,
        deferred_extractions=deferred,
    )
    assert extracted == {url: failure}
    assert list(deferred) == [url]
    assert len(submitted) == 1


def test_drain_interrupted_fetches_caches_only_completed_results() -> None:
    completed_url = "https://example.org/completed"
    failed_url = "https://example.org/failed"
    cancelled_url = "https://example.org/cancelled"
    rows = {url: {} for url in (completed_url, failed_url, cancelled_url)}
    pending = {url: [(row, "website")] for url, row in rows.items()}
    completed: Future[FetchResult] = Future()
    completed.set_result(
        FetchResult("ok", completed_url, final_url=completed_url, body=b"durable result")
    )
    failed: Future[FetchResult] = Future()
    failed.set_exception(RuntimeError("fetch worker failed"))
    cancelled: Future[FetchResult] = Future()
    assert cancelled.cancel()
    futures = {completed_url: completed, failed_url: failed, cancelled_url: cancelled}
    cache = RecordingTextCache()

    _drain_interrupted_fetches(
        pending,
        futures,
        cache=cast(TextCache, cache),
        invocation_id="interrupted-run",
        extractor=_extract,
    )

    assert [(value.url, invocation) for value, invocation in cache.records] == [
        (completed_url, "interrupted-run")
    ]
    assert rows[completed_url] == {
        "website_text": "durable result",
        "website_word_count": 2,
        "website_text_status": "success",
    }
    assert rows[failed_url] == {}
    assert rows[cancelled_url] == {}


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


def test_completed_fetches_are_extracted_before_a_slow_earlier_url(tmp_path: Path) -> None:
    """A fast page is parsed while an earlier submitted request is still active."""
    slow_started = threading.Event()
    release_slow = threading.Event()
    extraction_order: list[str] = []

    def fetch(url: str) -> FetchResult:
        if url.endswith("/slow"):
            slow_started.set()
            assert release_slow.wait(timeout=3)
        else:
            assert slow_started.wait(timeout=3)
        return FetchResult("ok", url, final_url=url, body=url.rsplit("/", 1)[1].encode())

    def extract(body: bytes, *, url: str) -> TextExtraction:
        extraction_order.append(url)
        if url.endswith("/fast"):
            release_slow.set()
        value = body.decode()
        return TextExtraction("success", value, 1, None, "test")

    slow_url = "https://example.org/slow"
    fast_url = "https://example.org/fast"
    pending = {
        slow_url: [({}, "website")],
        fast_url: [({}, "website")],
    }
    rows = {url: references[0][0] for url, references in pending.items()}
    cache = TextCache(tmp_path / "cache.sqlite3")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = _submit_fetches(pending, fetch_pool=pool, fetcher=fetch)
            _record_fetches(
                pending,
                futures,
                cache=cache,
                invocation_id="run",
                extractor=extract,
            )
        assert extraction_order == [fast_url, slow_url]
        assert rows[fast_url]["website_text"] == "fast"
        assert rows[slow_url]["website_text"] == "slow"
    finally:
        cache.close()


def test_next_batch_fetch_starts_while_current_batch_is_being_extracted(
    tmp_path: Path,
) -> None:
    """A slow parse in one batch does not leave the fetch pool idle at its boundary."""
    next_fetch_started = threading.Event()
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    write_legacy_polygon_shard(
        shard,
        [
            legacy_polygon_row(
                polygon_id=f"source:way/{index}",
                website=f"https://example.org/{index}",
                contact=None,
            )
            for index in range(2)
        ],
    )

    def fetch(url: str) -> FetchResult:
        if url.endswith("/1"):
            next_fetch_started.set()
        return FetchResult("ok", url, final_url=url, body=url.encode())

    def extract(body: bytes, *, url: str) -> TextExtraction:
        if url.endswith("/0"):
            assert next_fetch_started.wait(timeout=2)
        value = body.decode()
        return TextExtraction("success", value, 1, None, "test")

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="prefetch",
        fetcher=fetch,
        extractor=extract,
        batch_rows=1,
        fetch_workers=2,
    )

    assert next_fetch_started.is_set()
    assert [row["website_text"] for row in pq.read_table(shard).to_pylist()] == [
        "https://example.org/0",
        "https://example.org/1",
    ]


def test_prefetched_enrichment_is_deterministic_across_completion_orders(
    tmp_path: Path,
) -> None:
    """Out-of-order fetch completion preserves Parquet rows and cache content."""
    source_indexes = [0, 1, 0, 2, 3, 1]

    def run_once(name: str, delays: dict[int, float]) -> tuple[bytes, list[tuple[object, ...]]]:
        run_dir = tmp_path / name
        shard = run_dir / "polygons" / "source.parquet"
        rows = [
            legacy_polygon_row(
                polygon_id=f"source:way/{index}",
                website=f"https://example.org/{source_index}",
                contact=None,
            )
            for index, source_index in enumerate(source_indexes)
        ]
        write_legacy_polygon_shard(shard, rows)

        def fetch(url: str) -> FetchResult:
            index = int(url.rsplit("/", 1)[1])
            time.sleep(delays[index])
            if index == 1:
                return FetchResult("fetch_error", url, message="temporary failure")
            return FetchResult("ok", url, final_url=url, body=f"stable page {index}".encode())

        cache_path = run_dir / "cache" / "text.sqlite3"
        enrich_polygon_shard(
            shard,
            cache_path=cache_path,
            invocation_id="deterministic-run",
            fetcher=fetch,
            extractor=_extract,
            batch_rows=2,
            fetch_workers=4,
        )
        with sqlite3.connect(cache_path) as connection:
            cache_rows = connection.execute(
                """SELECT url, status, text, word_count, final_url, message,
                          attempt_count, trafilatura_version, invocation_id
                   FROM website_text ORDER BY url"""
            ).fetchall()
        return shard.read_bytes(), cache_rows

    first = run_once("completion-order-a", {0: 0.03, 1: 0.01, 2: 0.02, 3: 0.0})
    second = run_once("completion-order-b", {0: 0.0, 1: 0.02, 2: 0.01, 3: 0.03})

    assert first == second


def test_default_process_pool_extractor_entry_point() -> None:
    fetched = FetchResult(
        "ok",
        "https://example.org/library",
        final_url="https://example.org/library",
        body=(
            b"<html><body><article><h1>Public Library</h1><p>"
            + (
                b"The library provides books, archives, meeting rooms, and services "
                b"for everyone in the local community. " * 8
            )
            + b"</p></article></body></html>"
        ),
        media_type="text/html",
    )

    extracted = _extract_default_fetch(
        fetched.requested_url, fetched, invocation_id="process-pool-test"
    )

    assert extracted.status == "success"
    assert extracted.text is not None
    assert "Public Library" in extracted.text
    assert extracted.invocation_id == "process-pool-test"
    with pytest.raises(ValueError, match="successful fetch has no body"):
        _extract_default_fetch(
            fetched.requested_url,
            FetchResult("ok", fetched.requested_url, final_url=fetched.requested_url, body=None),
            invocation_id="process-pool-test",
        )


def test_default_process_pool_extractor_preserves_fetch_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = FetchResult(
        "ok",
        "https://example.org/start",
        final_url="https://example.org/resolved",
        body=b"encoded html",
        charset="windows-1252",
        media_type="application/xhtml+xml",
    )
    observed: dict[str, object] = {}

    def extract(
        body: bytes,
        *,
        url: str,
        charset: str | None,
        media_type: str | None,
    ) -> TextExtraction:
        observed.update(body=body, url=url, charset=charset, media_type=media_type)
        return TextExtraction("success", "Town library", 2, None, "test-version")

    monkeypatch.setattr(enrich_module, "extract_main_text", extract)

    cached = _extract_default_fetch(fetched.requested_url, fetched, invocation_id="metadata-run")

    assert observed == {
        "body": b"encoded html",
        "url": "https://example.org/resolved",
        "charset": "windows-1252",
        "media_type": "application/xhtml+xml",
    }
    assert cached.url == fetched.requested_url
    assert cached.final_url == "https://example.org/resolved"
    assert cached.status == "success"
    assert cached.text == "Town library"
    assert cached.word_count == 2
    assert cached.trafilatura_version == "test-version"
    assert cached.invocation_id == "metadata-run"


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

    monkeypatch.setattr(checkpoint_storage, "BatchParquetSink", unexpected_row_sink)
    staged = tmp_path / "staged.parquet"
    store = enrichment_checkpoint_store()

    max_batch_rows = store.assemble((part,), staged, batch_rows=2, row_count=2)

    assert max_batch_rows == 2
    assert pq.read_schema(staged).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert [row["polygon_id"] for row in pq.read_table(staged).to_pylist()] == [
        "source:way/0",
        "source:way/1",
    ]
    repeated = tmp_path / "repeated.parquet"
    store.assemble((part,), repeated, batch_rows=2, row_count=2)
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
    fetcher = ConcurrentFetchRecorder()

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetcher,
        extractor=_extract,
    )

    output = pq.read_table(shard).to_pylist()
    assert fetcher.peak >= 2
    assert fetcher.peak <= DEFAULT_FETCH_WORKERS
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
    fetcher = ConcurrentFetchRecorder()

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetcher,
        extractor=_extract,
        fetch_workers=2,
    )

    assert fetcher.peak >= 2
    assert fetcher.peak <= 2


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
