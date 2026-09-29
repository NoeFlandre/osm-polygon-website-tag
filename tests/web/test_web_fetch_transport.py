"""Behavioral fetch tests using the hermetic in-memory HTTP transport."""

from __future__ import annotations

import socket
import urllib.error

import pytest
from tests.fixtures.memory_http import MemoryHTTPFixture

from osm_polygon_website_tag.web.web_fetch import fetch_html


def test_a_body_of_exactly_the_limit_is_accepted(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/edge", b"x" * 100, Content_Type="text/html")

    result = fetch_html(memory_http.url("/edge"), max_bytes=100)

    assert (result.status, result.body) == ("ok", b"x" * 100)


def test_the_fetch_fixture_works_when_socket_connections_are_forbidden(
    memory_http: MemoryHTTPFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_http.route("/offline", b"ok", Content_Type="text/plain")

    def refuse_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the fetch fixture opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse_socket)

    assert fetch_html(memory_http.url("/offline")).body == b"ok"


def test_a_body_over_the_limit_is_refused(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/big", b"x" * 101, Content_Type="text/html")

    result = fetch_html(memory_http.url("/big"), max_bytes=100)

    assert (result.status, result.message) == ("fetch_error", "response_too_large")


def test_a_transport_timeout_is_a_fetch_error(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/slow", TimeoutError("deadline"), Content_Type="text/plain")

    result = fetch_html(memory_http.url("/slow"), timeout_seconds=0.3)

    assert (result.status, result.message) == ("fetch_error", "TimeoutError")


def test_a_missing_page_reports_its_status(memory_http: MemoryHTTPFixture) -> None:
    result = fetch_html(memory_http.url("/nothing-here"))

    assert (result.status, result.message, result.body) == ("fetch_error", "http_404", None)


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
def test_media_type_controls_whether_a_page_is_kept(
    memory_http: MemoryHTTPFixture, content_type: str, message: str | None
) -> None:
    memory_http.route("/page", b"<p>hi</p>", Content_Type=content_type)

    result = fetch_html(memory_http.url("/page"))

    assert result.message == message
    assert result.media_type == (content_type.split(";")[0] if message is None else None)


def test_a_page_without_content_type_is_refused(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/bare", b"<p>hi</p>")

    result = fetch_html(memory_http.url("/bare"))

    assert (result.status, result.message, result.body) == (
        "fetch_error",
        "unsupported_content_type",
        None,
    )


def test_the_charset_parameter_is_reported(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/latin", b"caf\xe9", Content_Type="text/html; charset=ISO-8859-1")

    result = fetch_html(memory_http.url("/latin"))

    assert (result.charset, result.body) == ("ISO-8859-1", b"caf\xe9")


def test_relative_redirects_reach_the_final_url(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/start", b"", status=302, Location="/middle")
    memory_http.route("/middle", b"", status=301, Location="end")
    memory_http.route("/end", b"<p>done</p>", Content_Type="text/html")

    result = fetch_html(memory_http.url("/start"))

    assert result.status == "ok"
    assert result.final_url == memory_http.url("/end")
    assert result.requested_url == memory_http.url("/start")
    assert memory_http.requests == ["/start", "/middle", "/end"]


def test_a_redirect_chain_over_the_limit_stops(memory_http: MemoryHTTPFixture) -> None:
    for step in range(6):
        memory_http.route(f"/r{step}", b"", status=302, Location=f"/r{step + 1}")

    result = fetch_html(memory_http.url("/r0"), max_redirects=2)

    assert (result.status, result.message) == ("fetch_error", "redirect_limit")
    assert memory_http.requests == ["/r0", "/r1", "/r2"]


def test_a_redirect_without_location_is_an_error(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/lost", b"", status=302)

    assert fetch_html(memory_http.url("/lost")).message == "redirect_without_location"


def test_a_redirect_to_a_non_http_scheme_is_invalid(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/bad", b"", status=302, Location="ftp://example.org/file")

    result = fetch_html(memory_http.url("/bad"))

    assert (result.status, result.message) == ("invalid_url", "invalid_redirect")


def test_a_redirect_to_a_private_address_is_refused_before_a_request(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route("/hop", b"", status=302, Location="http://10.0.0.5/secret")

    result = fetch_html(memory_http.url("/hop"))

    assert (result.status, result.message) == ("unsafe_url", "unsafe_url")
    assert result.final_url == "http://10.0.0.5/secret"
    assert memory_http.requests == ["/hop"]


def test_a_transport_connection_error_is_a_fetch_error(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/refused", urllib.error.URLError("Connection refused"))

    result = fetch_html(memory_http.url("/refused"))

    assert (result.status, result.message) == ("fetch_error", "URLError")
