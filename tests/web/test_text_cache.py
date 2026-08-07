"""Persistent website-text cache contracts."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from osm_polygon_website_tag.web.text_cache import (
    DEFAULT_COMMIT_BATCH_SIZE,
    CachedText,
    TextCache,
)


def _result(
    *,
    url: str = "https://example.org",
    status: str = "success",
    text: str | None = "full text",
    count: int | None = 2,
) -> CachedText:
    return CachedText(
        url=url,
        status=status,
        text=text,
        word_count=count,
        final_url="https://example.org/",
        message=None,
        attempt_count=0,
        last_attempt_at="",
        trafilatura_version="2.1.0",
        invocation_id="",
    )


def _committed_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM website_text").fetchone()[0])
    finally:
        connection.close()


def test_cache_batches_commits_and_flushes_at_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    cache = TextCache(path)

    for index in range(2):
        cache.record(_result(url=f"https://example.org/{index}"), invocation_id="run-1")
    assert _committed_count(path) == 0

    cache.flush()
    assert _committed_count(path) == 2
    cache.close()


def test_cache_close_flushes_pending_mutations(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    cache = TextCache(path)
    cache.record(_result(url="https://example.org/one"), invocation_id="run-1")
    assert _committed_count(path) == 0

    cache.close()

    assert _committed_count(path) == 1


def test_cache_commit_batch_size_is_positive_and_bounded(tmp_path: Path) -> None:
    assert DEFAULT_COMMIT_BATCH_SIZE == 64
    with pytest.raises(ValueError):
        TextCache(tmp_path / "unused-text-cache.sqlite3", commit_batch_size=0)


def test_success_is_reused_across_invocations(tmp_path: Path) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")
    cache.record(_result(), invocation_id="run-1")

    reused = cache.get_reusable("https://example.org", invocation_id="run-2")

    assert reused is not None
    assert reused.status == "success"
    assert reused.text == "full text"
    assert reused.attempt_count == 1
    cache.close()


def test_failure_is_reused_only_within_same_invocation(tmp_path: Path) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")
    cache.record(
        _result(status="fetch_error", text=None, count=None),
        invocation_id="run-1",
    )

    assert cache.get_reusable("https://example.org", invocation_id="run-1") is not None
    assert cache.get_reusable("https://example.org", invocation_id="run-2") is None
    cache.close()


def test_bulk_reusable_lookup_filters_by_status_and_invocation(tmp_path: Path) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")
    cache.record(_result(url="https://example.org/success"), invocation_id="run-1")
    cache.record(
        _result(
            url="https://example.org/current-failure", status="fetch_error", text=None, count=None
        ),
        invocation_id="run-2",
    )
    cache.record(
        _result(
            url="https://example.org/prior-failure", status="fetch_error", text=None, count=None
        ),
        invocation_id="run-1",
    )

    reusable = cache.get_reusable_many(
        {
            "https://example.org/success",
            "https://example.org/current-failure",
            "https://example.org/prior-failure",
            "https://example.org/missing",
        },
        invocation_id="run-2",
    )

    assert set(reusable) == {
        "https://example.org/success",
        "https://example.org/current-failure",
    }
    assert reusable["https://example.org/success"].attempt_count == 1
    assert reusable["https://example.org/current-failure"].status == "fetch_error"
    cache.close()


def test_bulk_reusable_lookup_chunks_large_url_sets(tmp_path: Path) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")
    urls = {f"https://example.org/{index}" for index in range(600)}
    for url in urls:
        cache.record(_result(url=url), invocation_id="run-1")

    reusable = cache.get_reusable_many(urls, invocation_id="run-2")

    assert set(reusable) == urls
    cache.close()


def test_later_failure_attempt_increments_counter(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    cache = TextCache(path)
    failure = _result(status="fetch_error", text=None, count=None)
    cache.record(failure, invocation_id="run-1")
    cache.record(failure, invocation_id="run-2")
    cache.close()

    reopened = TextCache(path)
    value = reopened.get_reusable("https://example.org", invocation_id="run-2")

    assert value is not None
    assert value.attempt_count == 2
    reopened.close()


def test_full_text_is_persisted_without_truncation(tmp_path: Path) -> None:
    full = "word " * 1_000_000
    cache = TextCache(tmp_path / "text.sqlite3")
    cache.record(_result(text=full, count=1_000_000), invocation_id="run-1")

    value = cache.get_reusable("https://example.org", invocation_id="later")

    assert value is not None
    assert value.text == full
    assert value.word_count == 1_000_000
    cache.close()


def test_corrupt_database_is_quarantined_and_recreated(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    path.write_bytes(b"not a valid sqlite database")

    cache = TextCache(path)
    cache.record(_result(), invocation_id="run-1")

    assert cache.get_reusable("https://example.org", invocation_id="run-2") is not None
    assert len(list(tmp_path.glob("text.sqlite3.corrupt-*"))) == 1
    cache.close()


def test_record_retries_after_a_transient_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    cache = TextCache(path)
    cache._db.execute("PRAGMA busy_timeout=1")
    holder = sqlite3.connect(path, check_same_thread=False)
    holder.execute("BEGIN")
    holder.execute("SELECT count(*) FROM website_text").fetchone()

    def release() -> None:
        time.sleep(0.15)
        holder.commit()
        holder.close()

    thread = threading.Thread(target=release)
    thread.start()
    cache.record(_result(), invocation_id="run-1")
    thread.join()

    assert cache.get_reusable("https://example.org", invocation_id="run-2") is not None
    cache.close()
