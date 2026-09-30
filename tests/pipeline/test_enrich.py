"""Transactional, resumable polygon-shard text enrichment."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

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
    """Assembly writes Arrow batches without materializing every row 