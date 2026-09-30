"""Socket-backed behaviour checks confined to one test-local loopback port."""

from __future__ import annotations

import time

import pytest
from tests.web.conftest import LoopbackHTTPFixture

from osm_polygon_website_tag.web.web_fetch import fetch_html


@pytest.mark.parametrize(
    ("body", "max_bytes", "expected_status", "expected_message"),
    [
        (b"page", 4, "ok", None),
        (b"pages", 4, "fetch_error", "response_too_large"),
    ],
)
def test_socket_transport_enforces_the_exact_body_limit(
    loopback_http: LoopbackHTTPFixture,
    body: bytes,
    max_bytes: int,
    expected_status: str,
    expected_message: str | None,
) -> None:
    loopback_http.route("/body", body, headers={"Content-Type": "text/html"})

    result = fetch_html(
        loopback_http.url("/body"), max_bytes=max_bytes, resolver=loopback_http.resolve
    )

    assert (result.status, result.message) == (expected_status, expected_message)
    assert loopback_http.requests == ["/body"]


def test_socket_transport_reads_relative_redirects(loopback_http: LoopbackHTTPFixture) -> None:
    loopback_http.route("/old", b"", status=301, headers={"Location": "/new"})
    loopback_http.route("/new", b"ok", headers={"Content-Type": "text/html"})

    result = fetch_html(loopback_http.url("/old"), resolver=loopback_http.resolve)

    assert (result.status, result.final_url, result.body) == (
        "ok",
        loopback_http.url("/new"),
        b"ok",
    )
    assert loopback_http.requests == ["/old", "/new"]


def test_socket_transport_stops_at_the_redirect_limit(loopback_http: LoopbackHTTPFixture) -> None:
    for index in range(4):
        loopback_http.route(f"/r{index}", b"", status=302, headers={"Location": f"/r{index + 1}"})

    result = fetch_html(loopback_http.url("/r0"), max_redirects=2, resolver=loopback_http.resolve)

    assert (result.status, result.message) == ("fetch_error", "redirect_limit")
    assert loopback_http.requests == ["/r0", "/r1", "/r2"]


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_media_type"),
    [
        ({}, "fetch_error", None),
        ({"Content-Type": "text/html; charset=utf-8"}, "ok", "text/html"),
        ({"Content-Type": "application/xhtml+xml"}, "ok", "application/xhtml+xml"),
    ],
)
def test_socket_transport_checks_content_type(
    loopback_http: LoopbackHTTPFixture,
    headers: dict[str, str],
    expected_status: str,
    expected_media_type: str | None,
) -> None:
    loopback_http.route("/type", b"<p>ok</p>", headers=headers)

    result = fetch_html(loopback_http.url("/type"), resolver=loopback_http.resolve)

    assert (result.status, result.media_type) == (expected_status, expected_media_type)


def test_socket_transport_enforces_a_whole_request_deadline(
    loopback_http: LoopbackHTTPFixture,
) -> None:
    loopback_http.route(
        "/slow",
        b"abcdef",
        headers={"Content-Type": "text/html"},
        drip_interval=0.1,
    )
    started = time.monotonic()

    result = fetch_html(
        loopback_http.url("/slow"), timeout_seconds=0.25, resolver=loopback_http.resolve
    )

    elapsed = time.monotonic() - started
    assert (result.status, result.message) == ("fetch_error", "TimeoutError")
    assert elapsed < 1.0


def test_socket_peer_is_the_validated_loopback_address(
    loopback_http: LoopbackHTTPFixture,
) -> None:
    loopback_http.route("/peer", b"ok", headers={"Content-Type": "text/html"})

    result = fetch_html(loopback_http.url("/peer"), resolver=loopback_http.resolve)

    assert result.status == "ok"
    assert loopback_http.resolved_addresses
    assert set(loopback_http.resolved_addresses) == {"127.0.0.1"}
    assert loopback_http.connected_addresses
    assert set(loopback_http.connected_addresses) == {("127.0.0.1", loopback_http.port)}


def test_socket_transport_rejects_redirect_to_another_loopback_port(
    loopback_http: LoopbackHTTPFixture,
) -> None:
    loopback_http.route(
        "/hop",
        b"",
        status=302,
        headers={"Location": f"http://127.0.0.1:{loopback_http.port + 1}/private"},
    )

    result = fetch_html(loopback_http.url("/hop"), resolver=loopback_http.resolve)

    assert (result.status, result.message) == ("unsafe_url", "unsafe_url")
    assert result.final_url == f"http://127.0.0.1:{loopback_http.port + 1}/private"
    assert loopback_http.requests == ["/hop"]
    assert set(loopback_http.connected_addresses) == {("127.0.0.1", loopback_http.port)}
