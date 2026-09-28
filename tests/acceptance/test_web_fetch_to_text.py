"""Acceptance: website values become extracted text through a real HTTP round trip.

The whole web half of the pipeline runs against a local HTTP server: fetch,
media-type and size checks, redirects, charset decoding, extraction, the text
cache, resume and language detection. Only the network is local; the SSRF guard
is widened to loopback by the ``loopback`` fixture and nowhere else.
"""

from __future__ import annotations

import functools
import ipaddress
import socket
from collections.abc import Sequence
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from tests.fixtures.loopback import LoopbackServer, drip
from tests.fixtures.polygon_shards import legacy_polygon_row, write_legacy_polygon_shard

from osm_polygon_website_tag.pipeline.detect_languages import detect_language_shard
from osm_polygon_website_tag.pipeline.enrich import enrich_polygon_shard
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.web.web_fetch import FetchResult, fetch_html


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any connection that is not to a loopback address."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object, *args: object) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if not ipaddress.ip_address(str(host)).is_loopback:
            raise AssertionError(f"connect to {host} outside loopback")
        real_connect(self, address, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)


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
    tmp_path: Path, loopback: LoopbackServer
) -> None:
    """Given UTF-8, Latin-1 (meta charset) and redirected pages, text is decoded correctly."""
    loopback.route("/utf8", _article("Zürich").encode(), Content_Type="text/html; charset=utf-8")
    loopback.route(
        "/latin",
        ('<meta charset="iso-8859-1">' + _article("café crème")).encode("latin-1"),
        Content_Type="text/html",
    )
    loopback.route("/old", b"", status=301, Location="/new")
    loopback.route("/new", _article("Redirected page").encode(), Content_Type="text/html")
    urls = [loopback.url(path) for path in ("/utf8", "/latin", "/old")]

    rows = _enrich(_shard(tmp_path, urls), tmp_path)

    assert [row["website_text_status"] for row in rows] == ["success"] * 3
    texts = [str(row["website_text"]) for row in rows]
    assert "Zürich" in texts[0]
    assert "café crème" in texts[1]
    assert "Redirected page" in texts[2]
    assert all(int(str(row["website_word_count"])) > 50 for row in rows)
    assert loopback.requests.count("/new") == 1


def test_given_unusable_pages_when_enriched_then_each_is_recorded_without_a_crash(
    tmp_path: Path, loopback: LoopbackServer
) -> None:
    """Given a 404, a PDF, an oversize page, a private redirect, a slow drip and a bad URL."""
    loopback.route("/pdf", b"%PDF-1.4", Content_Type="application/pdf")
    loopback.route("/huge", b"<p>" + b"x" * 5000 + b"</p>", Content_Type="text/html")
    loopback.route("/hop", b"", status=302, Location="http://10.0.0.5/secret")
    loopback.route("/slow", drip(200, 0.1), Content_Type="text/plain")
    urls = [
        loopback.url("/missing"),
        loopback.url("/pdf"),
        loopback.url("/huge"),
        loopback.url("/hop"),
        loopback.url("/slow"),
        "ftp://example.org/file",
    ]
    fetcher = functools.partial(fetch_html, max_bytes=1000, timeout_seconds=0.5)

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
    assert "/secret" not in loopback.requests


def test_given_a_populated_cache_when_run_again_then_no_request_is_made(
    tmp_path: Path, loopback: LoopbackServer
) -> None:
    """Given fetched pages, a second shard with the same URLs is served from the cache."""
    urls = []
    for name in ("a", "b", "c"):
        loopback.route(f"/{name}", _article(name.upper() * 4).encode(), Content_Type="text/html")
        urls.append(loopback.url(f"/{name}"))
    first = _enrich(_shard(tmp_path, urls, "first"), tmp_path)
    requests_after_first = list(loopback.requests)

    second = _enrich(_shard(tmp_path, urls, "second"), tmp_path)

    assert loopback.requests == requests_after_first
    assert [row["website_text"] for row in second] == [row["website_text"] for row in first]


def test_given_an_interrupted_run_when_resumed_then_only_the_rest_is_fetched(
    tmp_path: Path, loopback: LoopbackServer
) -> None:
    """Given a crash in the second batch, the completed prefix is not fetched again."""
    paths = [f"/p{index}" for index in range(1, 7)]
    for path in paths:
        loopback.route(
            path, _article(path.strip("/").upper() * 3).encode(), Content_Type="text/html"
        )
    urls = [loopback.url(path) for path in paths]
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
    served_before_resume = len(loopback.requests)

    resumed = _enrich(shard, tmp_path / "interrupted", batch_rows=2, fetch_workers=1)

    resumed_requests = loopback.requests[served_before_resume:]
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
    tmp_path: Path, loopback: LoopbackServer
) -> None:
    """Given three pages, detection writes one language and probability per polygon."""
    for name, marker in (("en", "English"), ("fr", "Bonjour"), ("de", "Guten")):
        loopback.route(f"/{name}", _article(marker * 3).encode(), Content_Type="text/html")
    shard = _shard(tmp_path, [loopback.url(f"/{name}") for name in ("en", "fr", "de")])
    _enrich(shard, tmp_path)

    detect_language_shard(shard, detector=_KeywordDetector())

    rows = pq.read_table(shard).to_pylist()
    assert [row["website_language"] for row in rows] == ["eng_Latn", "fra_Latn", "deu_Latn"]
    assert all(row["website_language_probability"] == pytest.approx(0.9) for row in rows)


def test_the_socket_guard_refuses_anything_but_loopback() -> None:
    """Guard the guard: a connect outside loopback fails the test instead of leaving the box."""
    with socket.socket() as probe, pytest.raises(AssertionError, match="outside loopback"):
        probe.connect(("93.184.216.34", 80))
