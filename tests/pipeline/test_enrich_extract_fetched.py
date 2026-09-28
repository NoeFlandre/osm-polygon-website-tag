"""Exact CachedText produced from one fetch result, including the HTTP charset."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from osm_polygon_website_tag.pipeline import enrich
from osm_polygon_website_tag.pipeline.enrich import _accepts_keyword, _extract_fetched
from osm_polygon_website_tag.web.text_cache import CachedText
from osm_polygon_website_tag.web.text_extract import TextExtraction
from osm_polygon_website_tag.web.web_fetch import FetchResult

URL = "https://example.org"


def _recording_extractor(
    calls: list[tuple[bytes, dict[str, object]]],
) -> Callable[..., TextExtraction]:
    def extract(html: bytes, **kwargs: object) -> TextExtraction:
        calls.append((html, kwargs))
        return TextExtraction("success", "Café crème", 2, "note", "2.1.0")

    return extract


def test_failed_fetch_is_cached_without_extraction() -> None:
    calls: list[tuple[bytes, dict[str, object]]] = []
    fetched = FetchResult("fetch_error", URL, final_url=URL + "/x", message="http_404")

    cached = _extract_fetched(
        URL, fetched, invocation_id="run", extractor=_recording_extractor(calls)
    )

    assert calls == []
    assert cached == CachedText(
        URL, "fetch_error", None, None, URL + "/x", "http_404", 0, "", None, "run"
    )


def test_ok_fetch_without_body_is_not_extracted() -> None:
    calls: list[tuple[bytes, dict[str, object]]] = []
    fetched = FetchResult("ok", URL, final_url=URL, body=None, message="m")

    cached = _extract_fetched(
        URL, fetched, invocation_id="run", extractor=_recording_extractor(calls)
    )

    assert calls == []
    assert cached == CachedText(URL, "ok", None, None, URL, "m", 0, "", None, "run")


def test_header_charset_is_forwarded_to_the_extractor() -> None:
    calls: list[tuple[bytes, dict[str, object]]] = []
    body = "Café crème".encode("cp1252")
    fetched = FetchResult("ok", URL, final_url=URL + "/final", body=body, charset="windows-1252")

    cached = _extract_fetched(
        URL, fetched, invocation_id="run", extractor=_recording_extractor(calls)
    )

    assert calls == [(body, {"url": URL + "/final", "charset": "windows-1252"})]
    assert cached == CachedText(
        URL, "success", "Café crème", 2, URL + "/final", "note", 0, "", "2.1.0", "run"
    )


def test_missing_charset_keeps_the_two_argument_extractor_call() -> None:
    calls: list[tuple[bytes, dict[str, object]]] = []
    fetched = FetchResult("ok", URL, final_url=None, body=b"<p>x</p>")

    cached = _extract_fetched(
        URL, fetched, invocation_id="run", extractor=_recording_extractor(calls)
    )

    assert calls == [(b"<p>x</p>", {"url": URL})]
    assert cached.final_url == URL


def test_url_only_extractor_still_works_when_a_charset_is_known() -> None:
    seen: list[str] = []

    def url_only(html: bytes, *, url: str) -> TextExtraction:
        seen.append(url)
        return TextExtraction("success", "x", 1, None, "2.1.0")

    fetched = FetchResult("ok", URL, final_url=URL, body=b"x", charset="koi8-r")

    cached = _extract_fetched(URL, fetched, invocation_id="run", extractor=url_only)

    assert seen == [URL]
    assert cached.status == "success"


def test_accepts_charset_reads_the_extractor_signature() -> None:
    result = TextExtraction("success", "x", 1, None, "2.1.0")

    def named(html: bytes, *, url: str, charset: str | None = None) -> TextExtraction:
        return result

    def keywords(html: bytes, **kwargs: object) -> TextExtraction:
        return result

    def url_only(html: bytes, *, url: str) -> TextExtraction:
        return result

    assert _accepts_keyword(named, "charset")
    assert _accepts_keyword(keywords, "charset")
    assert not _accepts_keyword(url_only, "charset")


def test_uninspectable_extractor_is_called_without_charset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_signature(_callable: object) -> None:
        raise ValueError("no signature found")

    monkeypatch.setattr(enrich.inspect, "signature", no_signature)

    def keywords(html: bytes, **kwargs: object) -> TextExtraction:
        return TextExtraction("success", "x", 1, None, "2.1.0")

    assert not _accepts_keyword(keywords, "charset")


def test_positional_only_charset_is_not_forwarded() -> None:
    result = TextExtraction("success", "x", 1, None, "2.1.0")

    def positional(html: bytes, charset: str | None = None, /, *, url: str) -> TextExtraction:
        return result

    def keyword_only(html: bytes, *, url: str, charset: str | None = None) -> TextExtraction:
        return result

    def positional_or_keyword(
        html: bytes, charset: str | None = None, *, url: str
    ) -> TextExtraction:
        return result

    def variadic(html: bytes, *charset: str, url: str) -> TextExtraction:
        return result

    assert not _accepts_keyword(positional, "charset")
    assert not _accepts_keyword(variadic, "charset")
    assert _accepts_keyword(keyword_only, "charset")
    assert _accepts_keyword(positional_or_keyword, "charset")
    fetched = FetchResult("ok", URL, final_url=URL, body=b"x", charset="koi8-r")
    assert (
        _extract_fetched(URL, fetched, invocation_id="run", extractor=positional).status
        == "success"
    )


def test_media_type_is_forwarded_only_to_extractors_that_take_it() -> None:
    seen: list[dict[str, object]] = []

    def full(
        html: bytes, *, url: str, charset: str | None = None, media_type: str | None = None
    ) -> TextExtraction:
        seen.append({"charset": charset, "media_type": media_type})
        return TextExtraction("success", "x", 1, None, "2.1.0")

    fetched = FetchResult(
        "ok", URL, final_url=URL, body=b"x", charset="utf-8", media_type="text/plain"
    )
    _extract_fetched(URL, fetched, invocation_id="run", extractor=full)

    assert seen == [{"charset": "utf-8", "media_type": "text/plain"}]

    def url_only(html: bytes, *, url: str) -> TextExtraction:
        return TextExtraction("success", "x", 1, None, "2.1.0")

    assert not _accepts_keyword(url_only, "media_type")
