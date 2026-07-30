"""Persistent website-text cache contracts."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_website_tag.web.text_cache import CachedText, TextCache


def _result(
    *,
    status: str = "success",
    text: str | None = "full text",
    count: int | None = 2,
) -> CachedText:
    return CachedText(
        url="https://example.org",
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
