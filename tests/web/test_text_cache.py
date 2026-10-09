"""Persistent website-text cache contracts."""

from __future__ import annotations

import dataclasses
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_website_tag.web import text_cache
from osm_polygon_website_tag.web.text_cache import (
    _LOCK_RETRY_DELAY_SECONDS,
    DEFAULT_COMMIT_BATCH_SIZE,
    CachedText,
    TextCache,
    _cached_text_from_row,
    _is_corruption_error,
    _is_locked_error,
    _quarantine_corrupt_database,
    _retry_locked,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [("database is locked", True), ("database table is locked", True), ("other error", False)],
)
def test_is_locked_error_recognizes_sqlite_lock_messages(message: str, expected: bool) -> None:
    assert _is_locked_error(sqlite3.OperationalError(message)) is expected


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


def test_commit_batch_size_zero_names_the_bound(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"^commit_batch_size must be positive$"):
        TextCache(tmp_path / "unused-text-cache.sqlite3", commit_batch_size=0)


def test_commit_batch_size_one_commits_every_record(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    cache = TextCache(path, commit_batch_size=1)

    cache.record(_result(url="https://example.org/one"), invocation_id="run-1")

    assert _committed_count(path) == 1
    cache.close()


def test_the_batch_commits_exactly_when_it_is_full(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    cache = TextCache(path, commit_batch_size=2)

    cache.record(_result(url="https://example.org/one"), invocation_id="run-1")
    assert _committed_count(path) == 0
    cache.record(_result(url="https://example.org/two"), invocation_id="run-1")
    assert _committed_count(path) == 2
    cache.record(_result(url="https://example.org/three"), invocation_id="run-1")
    assert _committed_count(path) == 2
    cache.close()


def test_the_cache_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "text.sqlite3"

    TextCache(path).close()
    TextCache(path).close()

    assert path.is_file()


def test_reusable_lookup_returns_none_for_an_unknown_url(tmp_path: Path) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")

    assert cache.get_reusable("https://unknown.example", invocation_id="run-1") is None

    cache.close()


def test_cached_text_is_immutable() -> None:
    value = _result()

    attribute = "status"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, attribute, "failure")


def test_invalid_cache_status_names_the_status(tmp_path: Path) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")

    with pytest.raises(ValueError, match=r"^invalid cache status: 'bogus'$"):
        cache.record(_result(status="bogus"), invocation_id="run-1")

    cache.close()


def test_record_reports_an_upsert_that_returns_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")

    class NoRow:
        def fetchone(self) -> None:
            return None

    class EmptyUpsert:
        def execute(self, *_args: object) -> NoRow:
            return NoRow()

    monkeypatch.setattr(cache, "_db", EmptyUpsert())
    with pytest.raises(AssertionError, match=r"^cache upsert did not return a row$"):
        cache.record(_result(), invocation_id="run-1")

    monkeypatch.undo()
    cache.close()


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


def test_record_uses_single_upsert_without_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")
    cache.record(_result(text="first", count=1), invocation_id="run-1")

    def unexpected_lookup(url: str) -> CachedText | None:
        raise AssertionError(f"record unexpectedly looked up {url}")

    monkeypatch.setattr(cache, "_get", unexpected_lookup)
    updated = cache.record(_result(text="second", count=2), invocation_id="run-2")

    assert updated.text == "second"
    assert updated.word_count == 2
    assert updated.attempt_count == 2
    assert updated.invocation_id == "run-2"
    assert datetime.fromisoformat(updated.last_attempt_at).tzinfo is not None
    cache.close()


@pytest.mark.parametrize("status", ["absent", "pending"])
def test_record_rejects_nonterminal_text_statuses(status: str, tmp_path: Path) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")

    with pytest.raises(ValueError, match="invalid cache status"):
        cache.record(_result(status=status, text=None, count=None), invocation_id="run-1")

    cache.close()


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


def test_retry_locked_retries_with_bounded_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = iter(
        [
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database table is locked"),
            "ok",
        ]
    )
    delays: list[float] = []

    monkeypatch.setattr("osm_polygon_website_tag.web.text_cache.time.sleep", delays.append)

    def operation() -> str:
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    result = _retry_locked(operation)

    assert result == "ok"
    assert delays == [0.1, 0.2]


def test_closing_twice_flushes_once_and_disables_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")
    assert cache._closed is False
    flushes = 0
    original_flush = cache.flush

    def counting_flush() -> None:
        nonlocal flushes
        flushes += 1
        original_flush()

    monkeypatch.setattr(cache, "flush", counting_flush)
    cache.close()
    cache.close()

    assert flushes == 1
    with pytest.raises(sqlite3.ProgrammingError):
        cache.get_reusable("https://example.org/one", invocation_id="run-2")


def test_a_failed_flush_still_closes_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = TextCache(tmp_path / "text.sqlite3")

    def failing_flush() -> None:
        raise RuntimeError("flush failed")

    monkeypatch.setattr(cache, "flush", failing_flush)
    with pytest.raises(RuntimeError, match="flush failed"):
        cache.close()

    cache.close()  # already closed: a second call is a no-op, not a second flush


def test_cached_text_from_row_maps_every_column() -> None:
    row = (
        "https://example.org/a",
        "success",
        "body",
        3,
        "https://example.org/final",
        "note",
        2,
        "2026-01-01T00:00:00+00:00",
        "1.0",
        "run-1",
    )

    assert _cached_text_from_row(row) == CachedText(
        url="https://example.org/a",
        status="success",
        text="body",
        word_count=3,
        final_url="https://example.org/final",
        message="note",
        attempt_count=2,
        last_attempt_at="2026-01-01T00:00:00+00:00",
        trafilatura_version="1.0",
        invocation_id="run-1",
    )


def test_cached_text_from_row_keeps_absent_optional_columns_empty() -> None:
    row = ("https://example.org/a", "absent", None, None, None, None, 1, "ts", None, "run-1")

    cached = _cached_text_from_row(row)

    assert (cached.text, cached.word_count, cached.final_url, cached.message) == (None,) * 4
    assert cached.trafilatura_version is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("database disk image is malformed", True),
        ("file is not a database", True),
        ("database is locked", False),
        ("no such table: texts", False),
    ],
)
def test_is_corruption_error_recognizes_each_sqlite_message(message: str, expected: bool) -> None:
    assert _is_corruption_error(sqlite3.DatabaseError(message)) is expected


def test_retry_locked_returns_after_transient_locks_with_doubling_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(text_cache.time, "sleep", sleeps.append)
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise sqlite3.OperationalError("database is locked")
        return "done"

    assert _retry_locked(operation) == "done"
    assert attempts == 3
    assert sleeps == [_LOCK_RETRY_DELAY_SECONDS, 2 * _LOCK_RETRY_DELAY_SECONDS]


def test_retry_locked_gives_up_after_exactly_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(text_cache.time, "sleep", sleeps.append)
    attempts = 0

    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        _retry_locked(operation)
    # Five retries, then one final attempt that is not wrapped.
    assert attempts == 6
    assert len(sleeps) == 5


def test_retry_locked_does_not_retry_other_operational_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(text_cache.time, "sleep", sleeps.append)
    attempts = 0

    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("no such table: texts")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _retry_locked(operation)
    assert attempts == 1
    assert sleeps == []


def test_quarantine_moves_the_database_and_each_sidecar_aside(tmp_path: Path) -> None:
    database = tmp_path / "text.sqlite3"
    database.write_bytes(b"corrupt")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{database}{suffix}").write_bytes(suffix.encode())

    quarantine = _quarantine_corrupt_database(database)

    pattern = r"text\.sqlite3\.corrupt-\d{8}T\d{6}\d{6}Z-[0-9a-f]{32}"
    assert re.fullmatch(pattern, quarantine.name)
    assert not database.exists()
    assert quarantine.read_bytes() == b"corrupt"
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{database}{suffix}").exists()
        assert Path(f"{quarantine}{suffix}").read_bytes() == suffix.encode()


def test_quarantine_without_sidecars_moves_only_the_database(tmp_path: Path) -> None:
    database = tmp_path / "text.sqlite3"
    database.write_bytes(b"corrupt")

    quarantine = _quarantine_corrupt_database(database)

    assert [path.name for path in tmp_path.iterdir()] == [quarantine.name]
    assert quarantine.read_bytes() == b"corrupt"


def test_both_opens_of_a_corrupt_cache_wait_thirty_seconds_for_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "text.sqlite3"
    path.write_bytes(b"not a valid sqlite database")
    real_connect = sqlite3.connect
    timeouts: list[float | None] = []

    def recording_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        timeouts.append(kwargs.get("timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    TextCache(path).close()

    # The first open finds the corrupt file; the reopen follows quarantine.
    assert timeouts == [30.0, 30.0]


def test_bulk_lookup_queries_at_most_256_urls_per_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    lookup_sizes: list[int] = []

    class RecordingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            if "WHERE url IN (" in sql:
                # The last two parameters are the status and the invocation id.
                lookup_sizes.append(len(parameters) - 2)
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: RecordingConnection(real_connect(*args, **kwargs)),
    )
    cache = TextCache(tmp_path / "text.sqlite3")
    urls = {f"https://example.org/{index}" for index in range(300)}

    cache.get_reusable_many(urls, invocation_id="run-2")

    assert lookup_sizes == [256, 44]
    cache.close()


def test_text_cache_exports_its_public_contract() -> None:
    assert set(text_cache.__all__) == {
        "CACHE_LOOKUP_CHUNK_SIZE",
        "DEFAULT_COMMIT_BATCH_SIZE",
        "CachedText",
        "TextCache",
    }
