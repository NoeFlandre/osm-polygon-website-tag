"""Transactional, resumable polygon-shard text enrichment."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import (
    legacy_polygon_row,
    write_legacy_polygon_shard,
)

import osm_polygon_website_tag.pipeline.checkpoint_storage as checkpoint_storage
import osm_polygon_website_tag.pipeline.enrich as enrich_module
from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_SCHEMA_VERSION
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.text_schema import TEXT_COLUMN_NAMES, initial_text_fields
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
    _ExtractPool,
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
        self.thread_names: set[str] = set()

    def __call__(self, url: str) -> FetchResult:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
            self.thread_names.add(threading.current_thread().name)
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
    _validate_enrichment_settings(MAX_FETCH_WORKERS, 1)
    with pytest.raises(ValueError, match="fetch_workers"):
        _validate_enrichment_settings(0, 1)
    with pytest.raises(
        ValueError, match=rf"^fetch_workers must be between 1 and {MAX_FETCH_WORKERS}$"
    ):
        _validate_enrichment_settings(MAX_FETCH_WORKERS + 1, 1)
    with pytest.raises(ValueError, match=r"^batch_rows must be positive$"):
        _validate_enrichment_settings(1, 0)
    rows: list[dict[str, object]] = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert _skip_checkpointed_rows(rows, 1) == ([{"id": 2}, {"id": 3}], 0)
    assert _skip_checkpointed_rows(rows, len(rows)) == ([], 0)
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
    _mark_invalid_url(row, "website")
    assert row["website_text_status"] == "invalid_url"


def test_unchanged_current_shard_is_not_rewritten(tmp_path: Path) -> None:
    shard = tmp_path / "source.parquet"
    pq.write_table(pa.Table.from_pylist([_current_row(0)], schema=POLYGON_PUBLIC_SCHEMA), shard)
    original = shard.read_bytes()

    result = enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "cache.sqlite3",
        invocation_id="unchanged-run",
        fetcher=lambda url: pytest.fail(f"complete text unexpectedly fetched: {url}"),
        extractor=_extract,
    )

    assert result.changed is False
    assert result.max_batch_rows == 1
    assert shard.read_bytes() == original


def test_empty_current_shard_has_zero_changed_rows_and_batch_size(tmp_path: Path) -> None:
    shard = tmp_path / "empty.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), shard)

    result = enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "cache.sqlite3",
        invocation_id="empty-run",
        fetcher=lambda url: pytest.fail(f"empty shard unexpectedly fetched: {url}"),
        extractor=_extract,
    )

    assert result.changed is False
    assert result.row_count == 0
    assert result.max_batch_rows == 0


@pytest.mark.parametrize(
    ("legacy_schema", "has_checkpoint_part", "expected_changed"),
    [(True, False, True), (False, False, False), (False, True, True)],
    ids=["migrate-old-schema", "current-schema-no-parts", "resume-current-schema"],
)
def test_prepare_context_marks_migration_and_resume_as_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_schema: bool,
    has_checkpoint_part: bool,
    expected_changed: bool,
) -> None:
    shard = tmp_path / "source.parquet"
    if legacy_schema:
        write_legacy_polygon_shard(shard, [legacy_polygon_row()])
    else:
        pq.write_table(pa.Table.from_pylist([_current_row(0)], schema=POLYGON_PUBLIC_SCHEMA), shard)
    source_schema = pq.read_schema(shard)
    target_schema, target_version = enrich_module._enrichment_contract(source_schema)
    store = enrichment_checkpoint_store(target_schema, target_version)
    parts = (tmp_path / "checkpoint" / "part-00000000.parquet",) if has_checkpoint_part else ()
    checkpoint = checkpoint_storage.Checkpoint(
        directory=tmp_path / "checkpoint",
        parts=parts,
        completed_rows=0,
    )
    monkeypatch.setattr(
        store,
        "load",
        lambda *_args, **_kwargs: checkpoint,
    )
    monkeypatch.setattr(
        enrich_module,
        "enrichment_checkpoint_store",
        lambda *_args, **_kwargs: store,
    )

    context = enrich_module._prepare_enrichment_context(
        shard, tmp_path / "cache.sqlite3", workers=1, batch_rows=1
    )
    try:
        assert context.changed is expected_changed
        assert context.next_part_index == len(parts)
    finally:
        context.cache.close()


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


def test_apply_cached_results_reuses_hits_and_returns_only_misses() -> None:
    hit_url = "https://example.org/cached"
    miss_url = "https://example.org/new"
    website_row: dict[str, object] = {}
    contact_row: dict[str, object] = {}
    miss_row: dict[str, object] = {}
    pending = {
        hit_url: [(website_row, "website"), (contact_row, "contact_website")],
        miss_url: [(miss_row, "website")],
    }
    cached_value = CachedText(
        hit_url, "success", "reused words", 2, hit_url, None, 1, "etag", "2.1.0", "prior-run"
    )

    class Cache:
        def __init__(self) -> None:
            self.lookups: list[tuple[set[str], str]] = []

        def get_reusable_many(self, urls: set[str], *, invocation_id: str) -> dict[str, CachedText]:
            self.lookups.append((urls, invocation_id))
            return {hit_url: cached_value}

    cache = Cache()
    unresolved = _apply_cached_results(
        pending,
        {hit_url, miss_url},
        cache=cast(TextCache, cache),
        invocation_id="current-run",
    )

    assert cache.lookups == [({hit_url, miss_url}, "current-run")]
    assert unresolved == {miss_url: [(miss_row, "website")]}
    assert website_row == {
        "website_text": "reused words",
        "website_word_count": 2,
        "website_text_status": "success",
    }
    assert contact_row == {
        "contact_website_text": "reused words",
        "contact_website_word_count": 2,
        "contact_website_text_status": "success",
    }
    assert miss_row == {}


def test_queue_tag_marks_absent_invalid_and_complete_values_without_fetching() -> None:
    pending: dict[str, list[tuple[dict[str, object], str]]] = {}
    lookup: set[str] = set()

    absent: dict[str, object] = {"website": None, "contact_website_text": "kept"}
    _queue_tag(
        absent,
        value_column="website",
        field_prefix="website",
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

    empty: dict[str, object] = {"website": ""}
    _queue_tag(
        empty,
        value_column="website",
        field_prefix="website",
        pending=pending,
        lookup_urls=lookup,
    )
    assert empty == {
        "website": "",
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "absent",
    }

    invalid: dict[str, object] = {"website": "ftp://example.org", "website_text": "stale"}
    _queue_tag(
        invalid,
        value_column="website",
        field_prefix="website",
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
        pending=pending,
        lookup_urls=lookup,
    )

    assert complete["website_text"] == "cached text"
    assert lookup == set()
    assert pending == {}


def test_queue_tag_deduplicates_normalized_urls_across_text_fields() -> None:
    website_row: dict[str, object] = {"website": "https://EXAMPLE.org/library"}
    contact_row: dict[str, object] = {"contact_website": "https://example.org/library"}
    pending: dict[str, list[tuple[dict[str, object], str]]] = {}
    lookup: set[str] = set()

    for row, value_column, field_prefix in (
        (website_row, "website", "website"),
        (contact_row, "contact_website", "contact_website"),
    ):
        _queue_tag(
            row,
            value_column=value_column,
            field_prefix=field_prefix,
            pending=pending,
            lookup_urls=lookup,
        )

    normalized = "https://example.org/library"
    assert lookup == {normalized}
    assert pending == {normalized: [(website_row, "website"), (contact_row, "contact_website")]}


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

    _mark_invalid_url(row, "website")

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
    assert all(value.invocation_id == "run" for value, _invocation in cache.records)
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
    submitted: list[tuple[object, ...]] = []
    fetched = FetchResult("ok", url, final_url=url, body=b"page text")

    class ExtractPool:
        def submit(self, url: str, fetched: FetchResult, invocation_id: str) -> Future[CachedText]:
            submitted.append((url, fetched, invocation_id))
            future: Future[CachedText] = Future()
            future.set_result(
                CachedText(url, "success", "page text", 2, url, None, 0, "", "2.1.0", "resolve-run")
            )
            return future

    extract_pool = ExtractPool()

    def fetch(value: str) -> FetchResult:
        fetched_urls.append(value)
        return fetched

    with ThreadPoolExecutor(max_workers=1) as pool:
        _resolve_pending(
            pending,
            cache=cast(TextCache, cache),
            invocation_id="resolve-run",
            fetcher=fetch,
            extractor=_extract,
            fetch_pool=pool,
            extract_pool=cast(_ExtractPool, extract_pool),
        )

    assert fetched_urls == [url]
    assert [(value.url, value.status, value.text) for value, _ in cache.records] == [
        (url, "success", "page text")
    ]
    assert cache.records[0][1] == "resolve-run"
    assert cache.records[0][0].invocation_id == "resolve-run"
    assert submitted == [(url, fetched, "resolve-run")]
    assert row == {
        "website_text": "page text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website_text": "page text",
        "contact_website_word_count": 2,
        "contact_website_text_status": "success",
    }


def test_resolve_pending_uses_custom_extractor_without_process_pool() -> None:
    url = "https://example.org/page"
    row: dict[str, object] = {}
    cache = RecordingTextCache()

    def fetch(value: str) -> FetchResult:
        return FetchResult("ok", value, final_url=value, body=b"custom extracted text")

    with ThreadPoolExecutor(max_workers=1) as pool:
        _resolve_pending(
            {url: [(row, "website")]},
            cache=cast(TextCache, cache),
            invocation_id="custom-extractor-run",
            fetcher=fetch,
            extractor=_extract,
            fetch_pool=pool,
        )

    assert row == {
        "website_text": "custom extracted text",
        "website_word_count": 3,
        "website_text_status": "success",
    }
    assert cache.records[0][0].text == "custom extracted text"


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


def test_record_fetched_applies_one_result_to_all_references() -> None:
    url = "https://example.org/shared"
    website_row: dict[str, object] = {}
    contact_row: dict[str, object] = {}
    cache = RecordingTextCache()

    _record_fetched(
        url,
        FetchResult("ok", url, final_url=url, body=b"shared page text"),
        [(website_row, "website"), (contact_row, "contact_website")],
        cache=cast(TextCache, cache),
        invocation_id="resume-run",
        extractor=_extract,
    )

    assert len(cache.records) == 1
    assert cache.records[0][0].text == "shared page text"
    assert cache.records[0][1] == "resume-run"
    assert cache.records[0][0].invocation_id == "resume-run"
    assert website_row["website_text"] == "shared page text"
    assert contact_row["contact_website_text"] == "shared page text"


def test_dispatch_extraction_defers_only_successful_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.org/page"
    successful = FetchResult("ok", url, final_url=url, body=b"page")
    expected = CachedText(url, "success", "page", 1, url, None, 0, "", "2.1.0", "run")
    submitted: list[tuple[object, ...]] = []

    class ExtractPool:
        def submit(self, url: str, fetched: FetchResult, invocation_id: str) -> Future[CachedText]:
            submitted.append((url, fetched, invocation_id))
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
        extract_pool=cast(_ExtractPool, pool),
        extracted=extracted,
        deferred_extractions=deferred,
    )
    assert extracted == {}
    assert deferred[url].result() == expected
    assert submitted == [(url, successful, "run")]

    failed = FetchResult("fetch_error", url, body=b"error response", message="http_503")
    failure = CachedText(url, "fetch_error", None, None, None, "http_503", 0, "", None, "run")
    monkeypatch.setattr(enrich_module, "_extract_fetched", lambda *_args, **_kwargs: failure)
    _dispatch_extraction(
        url,
        failed,
        invocation_id="run",
        extractor=_extract,
        extract_pool=cast(_ExtractPool, pool),
        extracted=extracted,
        deferred_extractions=deferred,
    )
    assert extracted == {url: failure}
    assert list(deferred) == [url]
    assert len(submitted) == 1

    no_body = FetchResult("ok", url, final_url=url, body=None)
    no_body_result = CachedText(url, "ok", None, None, url, None, 0, "", None, "run")
    monkeypatch.setattr(enrich_module, "_extract_fetched", lambda *_args, **_kwargs: no_body_result)
    _dispatch_extraction(
        url,
        no_body,
        invocation_id="run",
        extractor=_extract,
        extract_pool=cast(_ExtractPool, pool),
        extracted=extracted,
        deferred_extractions=deferred,
    )
    assert extracted[url] == no_body_result
    assert len(submitted) == 1


def test_drain_interrupted_fetches_caches_only_completed_results() -> None:
    completed_url = "https://example.org/completed"
    completed_error_url = "https://example.org/completed-error"
    failed_url = "https://example.org/failed"
    cancelled_url = "https://example.org/cancelled"
    late_url = "https://example.org/after-failure"
    rows = {
        url: {} for url in (completed_url, completed_error_url, failed_url, cancelled_url, late_url)
    }
    pending = {url: [(row, "website")] for url, row in rows.items()}
    completed: Future[FetchResult] = Future()
    completed.set_result(
        FetchResult("ok", completed_url, final_url=completed_url, body=b"durable result")
    )
    completed_error: Future[FetchResult] = Future()
    completed_error.set_result(FetchResult("fetch_error", completed_error_url, message="http_503"))
    failed: Future[FetchResult] = Future()
    failed.set_exception(RuntimeError("fetch worker failed"))
    cancelled: Future[FetchResult] = Future()
    assert cancelled.cancel()
    late: Future[FetchResult] = Future()
    late.set_result(FetchResult("ok", late_url, final_url=late_url, body=b"later result"))
    futures = {
        completed_url: completed,
        completed_error_url: completed_error,
        failed_url: failed,
        cancelled_url: cancelled,
        late_url: late,
    }
    cache = RecordingTextCache()

    _drain_interrupted_fetches(
        pending,
        futures,
        cache=cast(TextCache, cache),
        invocation_id="interrupted-run",
        extractor=_extract,
    )

    assert [
        (value.url, value.status, value.message, invocation) for value, invocation in cache.records
    ] == [
        (completed_url, "success", None, "interrupted-run"),
        (completed_error_url, "fetch_error", "http_503", "interrupted-run"),
        (late_url, "success", None, "interrupted-run"),
    ]
    assert rows[completed_url] == {
        "website_text": "durable result",
        "website_word_count": 2,
        "website_text_status": "success",
    }
    assert rows[completed_error_url] == {
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "fetch_error",
    }
    assert rows[failed_url] == {}
    assert rows[cancelled_url] == {}
    assert rows[late_url] == {
        "website_text": "later result",
        "website_word_count": 2,
        "website_text_status": "success",
    }


def test_private_enrichment_batch_and_future_helpers(tmp_path: Path) -> None:
    original = _current_row(1)
    original["website_text"] = None
    original["website_word_count"] = None
    original["website_text_status"] = "pending"
    states, pending, urls = _prepare_batch(
        [original],
        source_schema=POLYGON_PUBLIC_SCHEMA,
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


@pytest.mark.parametrize(
    ("website", "contact_website", "statuses", "normalized_url", "field_prefix"),
    [
        (
            "https://EXAMPLE.org/website",
            None,
            ("pending", "absent"),
            "https://example.org/website",
            "website",
        ),
        (
            None,
            "https://CONTACT.example.org/contact",
            ("absent", "pending"),
            "https://contact.example.org/contact",
            "contact_website",
        ),
    ],
    ids=["website-present", "contact-website-present"],
)
def test_prepare_batch_migrates_legacy_presence_and_keeps_change_snapshot(
    website: str | None,
    contact_website: str | None,
    statuses: tuple[str, str],
    normalized_url: str,
    field_prefix: str,
) -> None:
    original = legacy_polygon_row(website=website, contact=contact_website)

    states, pending, lookup_urls = _prepare_batch(
        [original],
        source_schema=POLYGON_PUBLIC_SCHEMA_V1_1,
    )

    assert len(states) == 1
    state = states[0]
    assert state.original is original
    assert state.row is not original
    assert (state.row["website_text_status"], state.row["contact_website_text_status"]) == statuses
    assert state.before == tuple(state.row.get(name) for name in TEXT_COLUMN_NAMES)
    assert lookup_urls == {normalized_url}
    assert pending == {normalized_url: [(state.row, field_prefix)]}


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
    with pytest.raises(ValueError, match=r"^successful fetch has no body$"):
        _extract_default_fetch(
            fetched.requested_url,
            FetchResult("ok", fetched.requested_url, final_url=fetched.requested_url, body=None),
            invocation_id="process-pool-test",
        )


def test_default_process_pool_extractor_rejects_success_without_body() -> None:
    url = "https://example.org/empty"
    fetched = FetchResult("ok", url, final_url=url, body=None)

    with pytest.raises(ValueError, match=r"^successful fetch has no body$"):
        _extract_default_fetch(url, fetched, invocation_id="empty-body-run")


@pytest.mark.parametrize(
    ("final_url", "charset", "media_type"),
    [
        pytest.param(
            "https://example.org/resolved",
            "windows-1252",
            "application/xhtml+xml",
            id="redirect-metadata",
        ),
        pytest.param(None, None, None, id="requested-url-fallback"),
    ],
)
def test_default_process_pool_extractor_uses_fetch_metadata(
    final_url: str | None,
    charset: str | None,
    media_type: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_url = "https://example.org/start"
    fetched = FetchResult(
        "ok",
        requested_url,
        final_url=final_url,
        body=b"encoded html",
        charset=charset,
        media_type=media_type,
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

    cached = _extract_default_fetch(requested_url, fetched, invocation_id="metadata-run")

    assert observed == {
        "body": b"encoded html",
        "url": final_url or requested_url,
        "charset": charset,
        "media_type": media_type,
    }
    assert cached.url == requested_url
    assert cached.final_url == (final_url or requested_url)
    assert cached.status == "success"
    assert cached.text == "Town library"
    assert cached.word_count == 2
    assert cached.trafilatura_version == "test-version"
    assert cached.invocation_id == "metadata-run"


def test_default_process_pool_extractor_preserves_extraction_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_url = "https://example.org/start"
    final_url = "https://example.org/final"
    fetched = FetchResult(
        "ok",
        requested_url,
        final_url=final_url,
        body=b"unextractable body",
        media_type="text/html",
    )
    monkeypatch.setattr(
        enrich_module,
        "extract_main_text",
        lambda *_args, **_kwargs: TextExtraction(
            "extract_error", None, None, "parser failed", "test-version"
        ),
    )

    cached = _extract_default_fetch(requested_url, fetched, invocation_id="failed-extract-run")

    assert cached == CachedText(
        requested_url,
        "extract_error",
        None,
        None,
        final_url,
        "parser failed",
        0,
        "",
        "test-version",
        "failed-extract-run",
    )


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
    assert result.shard_path == shard


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


def test_failed_fetch_cache_entry_is_reused_for_the_same_invocation(tmp_path: Path) -> None:
    url = "https://example.org/unavailable"
    invocation_id = "retryable-run"
    shard = tmp_path / "source.parquet"
    write_legacy_polygon_shard(shard, [legacy_polygon_row(website=url, contact=None)])
    cache_path = tmp_path / "cache.sqlite3"
    cache = TextCache(cache_path)
    cache.record(
        CachedText(
            url,
            "fetch_error",
            None,
            None,
            url,
            "http_503",
            1,
            "2026-10-01T00:00:00+00:00",
            None,
            invocation_id,
        ),
        invocation_id=invocation_id,
    )
    cache.close()
    fetched: list[str] = []

    def fetch(value: str) -> FetchResult:
        fetched.append(value)
        return FetchResult("fetch_error", value, final_url=value, message="http_503")

    enrich_polygon_shard(
        shard,
        cache_path=cache_path,
        invocation_id=invocation_id,
        fetcher=fetch,
        extractor=_extract,
    )

    row = pq.read_table(shard).to_pylist()[0]
    assert fetched == []
    assert row["website_text_status"] == "fetch_error"
    assert row["website_text"] is None


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
    assert all(name.startswith("website-fetch") for name in fetcher.thread_names)
    assert [row["website_text"] for row in output] == [
        f"text from https://example.org/{index}" for index in range(16)
    ]


def test_default_extraction_uses_bounded_process_pool_and_keeps_invocation_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    write_legacy_polygon_shard(
        shard,
        [
            legacy_polygon_row(
                polygon_id="source:way/1",
                website="https://example.org/library",
                contact=None,
            ),
            legacy_polygon_row(
                polygon_id="source:way/2",
                website="https://example.org/annex",
                contact=None,
            ),
        ],
    )
    html = (
        b"<html><body><article><h1>Public Library</h1><p>"
        + b"The library provides books, archives, meeting rooms, and services " * 8
        + b"for everyone in the local community.</p></article></body></html>"
    )
    pool_settings: list[tuple[int, str]] = []
    submitted: list[tuple[Callable[..., CachedText], tuple[object, ...]]] = []

    class InlineProcessPool:
        def __init__(
            self, *, max_workers: int, mp_context: BaseContext, initializer: Callable[[], None]
        ) -> None:
            assert initializer is enrich_module._import_extractor
            pool_settings.append((max_workers, mp_context.get_start_method()))

        def __enter__(self) -> InlineProcessPool:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def submit(self, function: Callable[..., CachedText], *args: object) -> Future[CachedText]:
            submitted.append((function, args))
            future: Future[CachedText] = Future()
            future.set_result(function(*args))
            return future

    monkeypatch.setattr(enrich_module, "ProcessPoolExecutor", InlineProcessPool)
    monkeypatch.setattr(enrich_module, "POOL_START_BYTES", 0)

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="native-pool-run",
        fetcher=lambda url: FetchResult("ok", url, final_url=url, body=html),
        batch_rows=1,
        fetch_workers=2,
    )

    rows = pq.read_table(shard).to_pylist()
    assert pool_settings == [(2, "spawn")]
    warmups = [item for item in submitted if item[0] is enrich_module._import_extractor]
    submitted = [item for item in submitted if item not in warmups]
    assert len(warmups) == 2
    assert len(submitted) == 2
    assert all(function is _extract_default_fetch for function, _args in submitted)
    assert {args[0] for _function, args in submitted} == {
        "https://example.org/library",
        "https://example.org/annex",
    }
    assert all(args[2] == "native-pool-run" for _function, args in submitted)
    assert all(row["website_text_status"] == "success" for row in rows)
    assert all("Public Library" in row["website_text"] for row in rows)
    with sqlite3.connect(tmp_path / "run" / "cache" / "text.sqlite3") as connection:
        invocation_ids = {
            invocation_id
            for (invocation_id,) in connection.execute("SELECT invocation_id FROM website_text")
        }
    assert invocation_ids == {"native-pool-run"}


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


def test_resume_rejects_a_checkpoint_for_a_replaced_source_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    rows = [
        legacy_polygon_row(
            polygon_id=f"source:way/{index}", website=f"https://example.org/{index}", contact=None
        )
        for index in range(2)
    ]
    write_legacy_polygon_shard(shard, rows)
    monkeypatch.setattr("osm_polygon_website_tag.pipeline.enrich.DEFAULT_FETCH_WORKERS", 1)
    calls: list[str] = []

    def interrupting_fetch(url: str) -> FetchResult:
        calls.append(url)
        if url.endswith("/1"):
            raise KeyboardInterrupt
        return FetchResult("ok", url, final_url=url, body=b"checkpointed source text")

    with pytest.raises(KeyboardInterrupt):
        enrich_polygon_shard(
            shard,
            cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
            invocation_id="one",
            fetcher=interrupting_fetch,
            extractor=_extract,
            batch_rows=1,
        )

    replacement_rows = [
        legacy_polygon_row(
            polygon_id=f"source:way/{index}",
            website=f"https://replacement.example/{index}",
            contact=None,
        )
        for index in range(2)
    ]
    write_legacy_polygon_shard(shard, replacement_rows)
    replacement_calls: list[str] = []

    def unexpected_fetch(url: str) -> FetchResult:
        replacement_calls.append(url)
        pytest.fail(f"source mismatch must reject the checkpoint before fetching: {url}")

    with pytest.raises(ValueError, match="checkpoint does not match source shard"):
        enrich_polygon_shard(
            shard,
            cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
            invocation_id="two",
            fetcher=unexpected_fetch,
            extractor=_extract,
            batch_rows=1,
        )

    assert calls == ["https://example.org/0", "https://example.org/1"]
    assert replacement_calls == []


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


def test_v1_4_enrichment_preserves_language_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    stores: list[checkpoint_storage.CheckpointStore] = []
    original_factory = enrich_module.enrichment_checkpoint_store

    def recording_factory(
        schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
        schema_version: str = SCHEMA_VERSION,
    ) -> checkpoint_storage.CheckpointStore:
        store = original_factory(schema, schema_version)
        stores.append(store)
        return store

    monkeypatch.setattr(enrich_module, "enrichment_checkpoint_store", recording_factory)

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "cache.sqlite3",
        invocation_id="run",
        fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"recovered text"),
        extractor=_extract,
    )

    result = pq.read_table(shard).to_pylist()[0]
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
    assert stores[0].schema_version == LANGUAGE_SCHEMA_VERSION
    assert result["schema_version"] == LANGUAGE_SCHEMA_VERSION
    assert result["website_language"] == "eng_Latn"
    assert result["website_language_probability"] == 0.93


def test_extract_pool_stays_inline_until_html_repays_worker_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[int] = []

    class RecordingPool:
        def __init__(self, *, max_workers: int, **_options: object) -> None:
            started.append(max_workers)

        def submit(self, function: Callable[..., CachedText], *args: object) -> Future[CachedText]:
            future: Future[CachedText] = Future()
            future.set_result(function(*args))
            return future

        def shutdown(self) -> None:
            started.append(-1)

    monkeypatch.setattr(enrich_module, "ProcessPoolExecutor", RecordingPool)
    monkeypatch.setattr(enrich_module, "POOL_START_BYTES", 10)
    calls: list[tuple[str, FetchResult, str]] = []

    def extract(url: str, fetched: FetchResult, invocation_id: str) -> CachedText:
        calls.append((url, fetched, invocation_id))
        if fetched.body == b"boom":
            raise ValueError("bad page")
        return CachedText(url, "success", "t", 1, url, None, 0, "", "v", invocation_id)

    monkeypatch.setattr(enrich_module, "_extract_default_fetch", extract)
    small = FetchResult("ok", "u", final_url="u", body=b"12345")
    with _ExtractPool(3) as pool:
        first = pool.submit("a", small, "run").result()
        assert (first.url, first.invocation_id) == ("a", "run")
        assert pool.submit("b", small, "run").result().url == "b"  # 10 bytes: still inline
        assert started == []
        assert pool.submit("c", small, "run").result().url == "c"  # 15 bytes: spawns
        assert started == [3]
        failing = FetchResult("ok", "u", final_url="u", body=b"boom")
        with pytest.raises(ValueError, match="bad page"):
            pool.submit("d", failing, "run").result()
    assert started == [3, -1]
    failing_call = ("d", failing, "run")
    assert calls == [("a", small, "run"), ("b", small, "run"), ("c", small, "run"), failing_call]
    with _ExtractPool(3) as idle, pytest.raises(ValueError, match="bad page"):
        idle.submit("e", failing, "run").result()
    assert started == [3, -1]
