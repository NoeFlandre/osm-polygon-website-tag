"""Fetching against a real socket: limits, deadlines, redirects and the connected peer."""

from __future__ import annotations

import socket
import time

import pytest
from tests.fixtures.loopback import LoopbackServer, drip, serve

from osm_polygon_website_tag.web import web_fetch
from osm_polygon_website_tag.web.politeness import HostPolicy
from osm_polygon_website_tag.web.web_fetch import fetch_html


def test_a_body_of_exactly_the_limit_is_accepted(loopback: LoopbackServer) -> None:
    loopback.route("/edge", b"x" * 100, Content_Type="text/html")

    result = fetch_html(loopback.url("/edge"), max_bytes=100)

    assert (result.status, result.body) == ("ok", b"x" * 100)


def test_a_body_one_byte_over_the_limit_is_refused_after_reading_no_more(
    loopback: LoopbackServer,
) -> None:
    loopback.route("/big", b"x" * 101, Content_Type="text/html")

    result = fetch_html(loopback.url("/big"), max_bytes=100)

    assert (result.status, result.message) == ("fetch_error", "response_too_large")


def test_a_streamed_body_over_the_limit_is_cut_off(loopback: LoopbackServer) -> None:
    loopback.route("/stream", lambda: iter([b"x" * 60, b"x" * 60]), Content_Type="text/plain")

    result = fetch_html(loopback.url("/stream"), max_bytes=100)

    assert result.message == "response_too_large"


def test_a_server_that_drips_bytes_hits_the_whole_request_deadline(
    loopback: LoopbackServer,
) -> None:
    loopback.route("/slow", drip(100, 0.1), Content_Type="text/plain")
    started = time.monotonic()

    result = fetch_html(loopback.url("/slow"), timeout_seconds=0.3)

    assert (result.status, result.message) == ("fetch_error", "TimeoutError")
    assert time.monotonic() - started < 6.0  # the drip would take 10 s; loaded runners are slow


def test_a_missing_page_is_not_downloaded_and_reports_its_status(
    loopback: LoopbackServer,
) -> None:
    result = fetch_html(loopback.url("/nothing-here"))

    assert (result.status, result.message) == ("fetch_error", "http_404")
    assert result.body is None


@pytest.mark.parametrize(
    ("content_type", "message"),
    [
        ("image/png", "unsupported_content_type"),
        ("application/pdf", "unsupported_content_type"),
        ("text/htmlx", "unsupported_content_type"),
        ("text/html; charset=utf-8", None),
        ("application/xhtml+xml", None),
        ("text/plain", None),
    ],
)
def test_the_media_type_decides_whether_a_page_is_kept(
    loopback: LoopbackServer, content_type: str, message: str | None
) -> None:
    loopback.route("/page", b"<p>hi</p>", Content_Type=content_type)

    result = fetch_html(loopback.url("/page"))

    assert result.message == message
    assert result.media_type == (content_type.split(";")[0] if message is None else None)


def test_a_page_without_a_content_type_is_still_accepted(loopback: LoopbackServer) -> None:
    loopback.route("/bare", b"<p>hi</p>")

    result = fetch_html(loopback.url("/bare"))

    assert (result.status, result.media_type, result.charset) == ("ok", None, None)


def test_the_charset_parameter_is_reported(loopback: LoopbackServer) -> None:
    loopback.route("/latin", b"caf\xe9", Content_Type="text/html; charset=ISO-8859-1")

    result = fetch_html(loopback.url("/latin"))

    assert (result.charset, result.body) == ("ISO-8859-1", b"caf\xe9")


def test_relative_redirects_are_followed_to_the_final_url(loopback: LoopbackServer) -> None:
    loopback.route("/start", b"", status=302, Location="/middle")
    loopback.route("/middle", b"", status=301, Location="end")
    loopback.route("/end", b"<p>done</p>", Content_Type="text/html")

    result = fetch_html(loopback.url("/start"))

    assert result.status == "ok"
    assert result.final_url == loopback.url("/end")
    assert result.requested_url == loopback.url("/start")
    assert loopback.requests == ["/start", "/middle", "/end"]


def test_a_redirect_chain_over_the_limit_stops_with_redirect_limit(
    loopback: LoopbackServer,
) -> None:
    for step in range(6):
        loopback.route(f"/r{step}", b"", status=302, Location=f"/r{step + 1}")

    result = fetch_html(loopback.url("/r0"), max_redirects=2)

    assert (result.status, result.message) == ("fetch_error", "redirect_limit")
    assert loopback.requests == ["/r0", "/r1", "/r2"]


def test_a_redirect_without_a_location_is_an_error(loopback: LoopbackServer) -> None:
    loopback.route("/lost", b"", status=302)

    assert fetch_html(loopback.url("/lost")).message == "redirect_without_location"


def test_a_redirect_to_a_non_http_scheme_is_invalid(loopback: LoopbackServer) -> None:
    loopback.route("/bad", b"", status=302, Location="ftp://example.org/file")

    result = fetch_html(loopback.url("/bad"))

    assert (result.status, result.message) == ("invalid_url", "invalid_redirect")


def test_a_redirect_to_a_private_address_is_refused_before_any_request(
    loopback: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    loopback.route("/hop", b"", status=302, Location="http://10.0.0.5/secret")

    result = fetch_html(loopback.url("/hop"))

    assert (result.status, result.message) == ("unsafe_url", "unsafe_url")
    assert result.final_url == "http://10.0.0.5/secret"
    assert loopback.requests == ["/hop"]


def test_the_connection_goes_to_the_address_that_was_validated(
    loopback: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name resolves once for validation and the socket layer resolves it again."""
    real = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: real(
            "127.0.0.1" if host == "pages.test" else host, *args, **kwargs
        ),
    )
    loopback.route("/", b"<p>hi</p>", Content_Type="text/html")
    validated: list[str] = []

    def resolver(host: str, port: int) -> list[tuple[object, ...]]:
        validated.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    result = fetch_html(
        f"http://pages.test:{loopback.port}/",
        resolver=resolver,
        request_once=web_fetch._download_once,
    )

    assert result.status == "ok"
    assert validated == ["pages.test"]
    assert loopback.peers == ["127.0.0.1"]


def test_a_dns_answer_that_changes_to_a_private_address_is_caught_at_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS rebinding: validation saw a public address, the socket sees a private one."""
    with serve() as private_server:
        private_server.route("/", b"<p>secret</p>", Content_Type="text/html")
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *_a, **_k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
            ],
        )

        def public(_host: str, port: int) -> list[tuple[object, ...]]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        result = fetch_html(f"http://rebind.test:{private_server.port}/", resolver=public)

    assert (result.status, result.message) == ("unsafe_url", "unsafe_url")
    assert result.body is None


def test_a_refused_connection_is_a_fetch_error_not_a_crash(loopback: LoopbackServer) -> None:
    with serve() as closed:
        port = closed.port
    result = fetch_html(f"http://127.0.0.1:{port}/", timeout_seconds=2)

    assert (result.status, result.message) == ("fetch_error", "URLError")


def test_an_environment_proxy_is_never_used(
    loopback: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    loopback.route("/", b"<p>direct</p>", Content_Type="text/html")

    result = fetch_html(loopback.url("/"))

    assert (result.status, result.body) == ("ok", b"<p>direct</p>")


def test_the_request_identifies_the_crawler(loopback: LoopbackServer) -> None:
    loopback.route("/", b"ok", Content_Type="text/plain")

    fetch_html(loopback.url("/"))

    assert loopback.agents == [web_fetch.USER_AGENT]
    assert web_fetch.USER_AGENT.startswith("osm-polygon-website-tag/")


def test_a_polite_fetcher_spaces_successive_requests_to_one_host(
    loopback: LoopbackServer,
) -> None:
    loopback.route("/", b"ok", Content_Type="text/plain")
    fetcher = web_fetch.make_polite_fetcher(HostPolicy(concurrency=1, delay_seconds=0.2))
    started = time.monotonic()

    results = [fetcher(loopback.url("/")), fetcher(loopback.url("/"))]

    assert [result.status for result in results] == ["ok", "ok"]
    assert time.monotonic() - started >= 0.2
