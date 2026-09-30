"""Robots rules are fetched through the safe, injected transport."""

from __future__ import annotations

import urllib.robotparser

import pytest
from tests.fixtures.memory_http import MemoryHTTPFixture

from osm_polygon_website_tag.web import web_fetch
from osm_polygon_website_tag.web.politeness import HostLimiter, HostPolicy
from osm_polygon_website_tag.web.web_fetch import FetchResult, fetch_html


def test_disallowed_page_is_not_requested(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route(
        "/robots.txt",
        b"User-agent: *\nDisallow: /private\n",
        Content_Type="text/plain",
    )
    memory_http.route("/private", b"secret", Content_Type="text/html")

    result = fetch_html(memory_http.url("/private"))

    assert result == FetchResult(
        "robots_disallowed",
        memory_http.url("/private"),
        final_url=memory_http.url("/private"),
        message="robots_disallowed",
    )
    assert memory_http.requests == ["/robots.txt"]


def test_policy_is_cached_per_origin_and_allowed_pages_are_fetched(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route(
        "/robots.txt",
        b"User-agent: *\nDisallow: /private\n",
        Content_Type="text/plain",
    )
    memory_http.route("/public/a", b"a", Content_Type="text/html")
    memory_http.route("/public/b", b"b", Content_Type="text/html")

    first = fetch_html(memory_http.url("/public/a"))
    second = fetch_html(memory_http.url("/public/b"))

    assert (first.status, second.status) == ("ok", "ok")
    assert memory_http.requests == ["/robots.txt", "/public/a", "/public/b"]


def test_missing_robots_file_allows_the_page(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/page", b"page", Content_Type="text/html")

    result = fetch_html(memory_http.url("/page"))

    assert result.status == "ok"
    assert memory_http.requests == ["/robots.txt", "/page"]


def test_unavailable_robots_file_fails_closed(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/robots.txt", b"busy", status=503, Content_Type="text/plain")
    memory_http.route("/page", b"page", Content_Type="text/html")

    result = fetch_html(memory_http.url("/page"))

    assert (result.status, result.message) == ("fetch_error", "robots_unavailable")
    assert memory_http.requests == ["/robots.txt"]


def test_oversized_robots_file_fails_closed(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/robots.txt", b"x" * (web_fetch.ROBOTS_MAX_BYTES + 1))
    memory_http.route("/page", b"page", Content_Type="text/html")

    result = fetch_html(memory_http.url("/page"))

    assert (result.status, result.message) == ("fetch_error", "robots_unavailable")
    assert memory_http.requests == ["/robots.txt"]


def test_parse_error_allows_the_page(
    memory_http: MemoryHTTPFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_parse(_parser: urllib.robotparser.RobotFileParser, _lines: list[str]) -> None:
        raise ValueError("malformed robots file")

    monkeypatch.setattr(web_fetch.urllib.robotparser.RobotFileParser, "parse", reject_parse)
    memory_http.route("/robots.txt", b"not a valid rules file", Content_Type="text/plain")
    memory_http.route("/page", b"page", Content_Type="text/html")

    result = fetch_html(memory_http.url("/page"))

    assert result.status == "ok"
    assert memory_http.requests == ["/robots.txt", "/page"]


def test_robots_redirect_to_private_address_is_blocked(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route("/robots.txt", b"", status=302, Location="http://127.0.0.1/robots.txt")
    memory_http.route("/page", b"page", Content_Type="text/html")

    result = fetch_html(memory_http.url("/page"))

    assert (result.status, result.message) == ("unsafe_url", "robots_unsafe_url")
    assert result.final_url == "http://127.0.0.1/robots.txt"
    assert memory_http.requests == ["/robots.txt"]


def test_crawl_delay_is_applied_to_page_requests(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route(
        "/robots.txt",
        b"User-agent: *\nCrawl-delay: 3\n",
        Content_Type="text/plain",
    )
    memory_http.route("/page", b"page", Content_Type="text/html")
    elapsed = [0.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        elapsed[0] += seconds

    limiter = HostLimiter(
        HostPolicy(concurrency=1, delay_seconds=0),
        clock=lambda: elapsed[0],
        sleep=sleep,
    )

    result = fetch_html(memory_http.url("/page"), limiter=limiter)

    assert result.status == "ok"
    assert waits == [3.0]
    assert memory_http.requests == ["/robots.txt", "/page"]
