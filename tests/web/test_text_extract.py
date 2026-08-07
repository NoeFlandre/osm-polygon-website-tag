"""Trafilatura adapter tests using static HTML only."""

from __future__ import annotations

from osm_polygon_website_tag.web import text_extract
from osm_polygon_website_tag.web.text_extract import extract_main_text


def test_extract_main_text_from_static_html() -> None:
    html = b"""
    <html><body>
      <nav>Navigation noise</nav>
      <main><article><h1>Public Library</h1>
      <p>This library serves the whole community with books and archives.</p>
      </article></main>
      <div class="comments">A visitor comment that must not be included.</div>
    </body></html>
    """

    result = extract_main_text(html, url="https://example.org/library")

    assert result.status == "success"
    assert result.text is not None
    assert "Public Library" in result.text
    assert "serves the whole community" in result.text
    assert result.word_count is not None
    assert result.word_count > 0


def test_trafilatura_version_lookup_is_cached(monkeypatch) -> None:
    """Repeated URL extraction must not rescan package metadata each time."""
    calls: list[str] = []

    def fake_version(name: str) -> str:
        calls.append(name)
        return "test-version"

    cached_version = getattr(text_extract, "_trafilatura_version", None)
    if cached_version is not None:
        cached_version.cache_clear()
    monkeypatch.setattr(text_extract, "version", fake_version)
    monkeypatch.setattr(text_extract.trafilatura, "extract", lambda *_args, **_kwargs: "text")
    try:
        first = extract_main_text(b"<html/>", url="https://example.org/one")
        second = extract_main_text(b"<html/>", url="https://example.org/two")
    finally:
        if cached_version is not None:
            cached_version.cache_clear()

    assert first.trafilatura_version == "test-version"
    assert second.trafilatura_version == "test-version"
    assert calls == ["trafilatura"]


def test_trafilatura_options_are_reused_per_thread(monkeypatch) -> None:
    """Repeated extraction reuses setup while updating the current URL."""
    state = getattr(text_extract, "_extractor_state", None)
    if state is not None:
        state.__dict__.clear()
    constructions: list[dict[str, object]] = []

    class FakeExtractor:
        def __init__(self, **kwargs: object) -> None:
            constructions.append(kwargs)
            self.url = kwargs.get("url")
            self.source = kwargs.get("url")

    seen_options: list[FakeExtractor] = []

    def fake_extract(*_args: object, **kwargs: object) -> str:
        option = kwargs["options"]
        assert isinstance(option, FakeExtractor)
        seen_options.append(option)
        return "text"

    monkeypatch.setattr(text_extract, "Extractor", FakeExtractor, raising=False)
    monkeypatch.setattr(text_extract.trafilatura, "extract", fake_extract)
    try:
        first = extract_main_text(b"<html/>", url="https://example.org/one")
        second = extract_main_text(b"<html/>", url="https://example.org/two")
    finally:
        if state is not None:
            state.__dict__.clear()

    assert first.text == second.text == "text"
    assert len(constructions) == 1
    assert seen_options[0] is seen_options[1]
    assert seen_options[1].url == "https://example.org/two"
    assert seen_options[1].source == "https://example.org/two"


def test_empty_trafilatura_result_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(text_extract.trafilatura, "extract", lambda *_args, **_kwargs: None)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.status == "empty"
    assert result.text == ""
    assert result.word_count == 0


def test_extractor_failure_is_sanitized(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("secret response body")

    monkeypatch.setattr(text_extract.trafilatura, "extract", fail)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.status == "extract_error"
    assert result.text is None
    assert result.word_count is None
    assert result.message == "RuntimeError"


def test_full_text_is_retained_without_truncation(monkeypatch) -> None:
    full = "word " * 1_000_000
    monkeypatch.setattr(text_extract.trafilatura, "extract", lambda *_args, **_kwargs: full)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.text == full
    assert result.word_count == 1_000_000
