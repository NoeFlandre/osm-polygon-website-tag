"""Exact CachedText produced from one fetch result, including the HTTP charset."""

from __future__ import annotations

from osm_polygon_website_tag.pipeline.enrich import _extract_fetched
from osm_polygon_website_tag.web.text_cache import CachedText
from osm_polygon_website_tag.web.text_extract import TextExtraction
from osm_polygon_website_tag.web.web_fetch import FetchResult

URL = "https://example.org"


def _recording_extractor(calls: list[tuple[bytes, dict[str, object]]]):
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
