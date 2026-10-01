"""Acceptance: loopback-fetched website values flow through extraction to text."""

from __future__ import annotations

import functools
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from tests.fixtures.loopback_http import LoopbackHTTPFixture
from tests.fixtures.polygon_shards import legacy_polygon_row, write_legacy_polygon_shard

from osm_polygon_website_tag.pipeline.detect_languages import detect_language_shard
from osm_polygon_website_tag.pipeline.enrich import enrich_polygon_shard
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.web.text_cache import TextCache
from osm_polygon_website_tag.web.web_fetch import FetchResult, fetch_html


def _article(topic: str, *, extra: str = "") -> str:
    sentence = f"{topic} is described here in plain words that a reader can follow easily. "
    return (
        "<html><head><title>Page</title></head><body><article>"
        + "".join(f"<p>{sentence * 3}{extra}</p>" for _ in range(6))
        + "</article></body></html>"
    )


def _shard(directory: Path, urls: Sequence[str], name: str = "source") -> Path:
    shard = directory / "polygons" / f"{name}.parquet"
    rows = [
        legacy_polygon_row(polygon_id=f"{name}:way/{index}", website=url, contact=None)
        for index, url in enumerate(urls, start=1)
    ]
    write_legacy_polygon_shard(shard, rows)
    return shard


def _enrich(shard: Path, directory: Path, **options: object) -> list[dict[str, object]]:
    enrich_polygon_shard(
        shard,
        cache_path=directory / "cache" / "website_text.sqlite3",
        invocation_id="acceptance",
        **options,  # ty: ignore[invalid-argument-type]
    )
    return pq.read_table(shard).to_pylist()


def test_given_reachable_pages_when_enriched_then_their_text_is_extracted(
    tmp_path: Path, acceptance_http: LoopbackHTTPFixture
) -> None:
    """Given UTF-8, Latin-1 (meta charset) and redirected pages, text is decoded correctly."""
    acceptance_http.route(
        "/utf8",
        _article("Zürich").encode(),
        headers={"Content-Type": "text/html; charset=utf-8"},
    )
    acceptance_http.route(
        "/latin",
        ('<meta charset="iso-8859-1">' + _article("café crème")).encode("latin-1"),
        headers={"Content-Type": "text/html"},
    )
    acceptance_http.route("/old", b"", status=301, headers={"Location": "/new"})
    acceptance_http.route(
        "/new",
        _article("Redirected page").encode(),
        headers={"Content-Type": "text/html"},
    )
    urls = [acceptance_http.url(path) for path in ("/utf8", "/latin", "/old")]

    rows = _enrich(_shard(tmp_path, urls), tmp_path)
    with closing(TextCache(tmp_path / "cache" / "website_text.sqlite3")) as cache:
        redirected = cache.get_reusable(urls[2], invocation_id="acceptance")

    assert [row["website_text_status"] for row in rows] == ["success"] * 3
    assert redirected is not None
    assert (redirected.status, redirected.final_url) == (
        "success",
        acceptance_http.url("/new"),
    )
    texts = [str(row["website_text"]) for row in rows]
    assert "Zürich" in texts[0]
    assert "café crème" in texts[1]
    assert "Redirected page" in texts[2]
    assert all(int(str(row["website_word_count"])) > 50 for row in rows)
    assert acceptance_http.requests.count("/new") == 1


def test_given_unusable_pages_when_enriched_then_each_is_recorded_without_a_crash(
    tmp_path: Path, acceptance_http: LoopbackHTTPFixture
) -> None:
    """Given unusable pages and a timeout, each fetch failure is recorded without a crash."""
    acceptance_http.route("/pdf", b"%PDF-1.4", headers={"Content-Type": "application/pdf"})
    acceptance_http.route(
        "/huge", b"<p>" + b"x" * 5000 + b"</p>", headers={"Content-Type": "text/html"}
    )
    acceptance_http.route("/hop", b"", status=302, headers={"Location": "http://10.0.0.5/secret"})
    acceptance_http.route(
        "/slow",
        b"slow response body",
        headers={"Content-Type": "text/plain"},
        drip_interval=0.1,
    )
    urls = [
        acceptance_http.url("/missing"),
        acceptance_http.url("/pdf"),
        acceptance_http.url("/huge"),
        acceptance_http.url("/hop"),
        acceptance_http.url("/slow"),
        "ftp://example.org/file",
    ]
    fetcher = functools.partial(fetch_html, max_bytes=1000, timeout_seconds=0.3)

    rows = _enrich(_shard(tmp_path, urls), tmp_path, fetcher=fetcher)

    assert [row["website_text_status"] for row in rows] == [
        "fetch_error",
        "fetch_error",
        "fetch_error",
        "unsafe_url",
        "fetch_error",
        "invalid_url",
    ]
    assert all(row["website_text"] is None for row in rows)
    assert "/secret" not in acceptance_http.requests


def test_given_a_populated_cache_when_run_again_then_no_request_is_made(
    tmp_path: Path, acceptance_http: LoopbackHTTPFixture
) -> None:
    """Given fetched pages, a second shard with the same URLs is served from the cache."""
    urls = []
    for name in ("a", "b", "c"):
        acceptance_http.route(
            f"/{name}",
            _article(name.upper() * 4).encode(),
            headers={"Content-Type": "text/html"},
        )
        urls.append(acceptance_http.url(f"/{name}"))
    first = _enrich(_shard(tmp_path, urls, "first"), tmp_path)
    requests_after_first = list(acceptance_http.requests)

    second = _enrich(_shard(tmp_path, urls, "second"), tmp_path)

    assert acceptance_http.requests == requests_after_first
    assert [row["website_text"] for row in second] == [row["website_text"] for row in first]


def test_given_an_interrupted_run_when_resumed_then_only_the_rest_is_fetched(
    tmp_path: Path, acceptance_http: LoopbackHTTPFixture
) -> None:
    """Given a crash in the second batch, the completed prefix is not fetched again."""
    paths = [f"/p{index}" for index in range(1, 7)]
    for path in paths:
        acceptance_http.route(
            path,
            _article(path.strip("/").upper() * 3).encode(),
            headers={"Content-Type": "text/html"},
        )
    urls = [acceptance_http.url(path) for path in paths]
    calls = 0

    def crashing(url: str) -> FetchResult:
        nonlocal calls
        calls += 1
        if calls == 4:  # the second URL of the second batch
            raise KeyboardInterrupt
        return fetch_html(url)

    shard = _shard(tmp_path / "interrupted", urls)
    with pytest.raises(KeyboardInterrupt):
        _enrich(shard, tmp_path / "interrupted", fetcher=crashing, batch_rows=2, fetch_workers=1)
    served_before_resume = len(acceptance_http.requests)

    resumed = _enrich(shard, tmp_path / "interrupted", batch_rows=2, fetch_workers=1)

    resumed_requests = acceptance_http.requests[served_before_resume:]
    assert "/p1" not in resumed_requests
    assert "/p2" not in resumed_requests
    assert set(resumed_requests) <= set(paths[2:])
    clean = _enrich(_shard(tmp_path / "clean", urls), tmp_path / "clean", batch_rows=2)
    assert [row["website_text"] for row in resumed] == [row["website_text"] for row in clean]
    assert all(row["website_text_status"] == "success" for row in resumed)


class _KeywordDetector:
    identity = ModelIdentity("repo", "file", "revision", "d" * 64)

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        labels = {"English": "eng_Latn", "Bonjour": "fra_Latn", "Guten": "deu_Latn"}
        return [
            LanguagePrediction(
                next((label for word, label in labels.items() if word in text), "und_Latn"), 0.9
            )
            for text in texts
        ]


def test_given_extracted_english_french_and_german_text_then_languages_are_filled(
    tmp_path: Path, acceptance_http: LoopbackHTTPFixture
) -> None:
    """Given three pages, detection writes one language and probability per polygon."""
    for name, marker in (("en", "English"), ("fr", "Bonjour"), ("de", "Guten")):
        acceptance_http.route(
            f"/{name}",
            _article(marker * 3).encode(),
            headers={"Content-Type": "text/html"},
        )
    shard = _shard(tmp_path, [acceptance_http.url(f"/{name}") for name in ("en", "fr", "de")])
    _enrich(shard, tmp_path)

    detect_language_shard(shard, detector=_KeywordDetector())

    rows = pq.read_table(shard).to_pylist()
    assert [row["website_language"] for row in rows] == ["eng_Latn", "fra_Latn", "deu_Latn"]
    assert all(row["website_language_probability"] == pytest.approx(0.9) for row in rows)
