"""Hermetic tests for bounded and SSRF-safe website downloads."""

from __future__ import annotations

import socket

import pytest

from osm_polygon_website_tag.web.web_fetch import (
    HttpResponse,
    UnsafeUrlError,
    fetch_html,
    normalize_http_url,
    validate_public_http_url,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.org/path", "https://example.org/path"),
        ("//example.org/path", "https://example.org/path"),
        ("HTTP://Example.ORG/a", "http://example.org/a"),
    ],
)
def test_normalize_http_url(raw: str, expected: str) -> None:
    assert normalize_http_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ftp://example.org",
        "mailto:test@example.org",
        "https://user:password@example.org",
        "https://localhost/",
    ],
)
def test_normalize_rejects_invalid_or_credentialed_urls(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_http_url(raw)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "10.0.0.1", "169.254.1.1", "224.0.0.1", "192.0.2.1"],
)
def test_validate_rejects_non_global_addresses(address: str) -> None:
    def resolve(_host: str, _port: int):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    with pytest.raises(UnsafeUrlError):
        validate_public_http_url("https://example.org", resolver=resolve)


def test_validate_accepts_global_address() -> None:
    def resolve(_host: str, _port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    assert validate_public_http_url("https://example.org", resolver=resolve)


def test_fetch_validates_every_redirect_before_request() -> None:
    requested: list[str] = []

    def request(url: str, _timeout: float, _max_bytes: int) -> HttpResponse:
        requested.append(url)
        return HttpResponse(302, {"location": "http://127.0.0.1/private"}, b"")

    result = fetch_html(
        "https://example.org",
        request_once=request,
        resolver=lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    assert result.status == "unsafe_url"
    assert requested == ["https://example.org"]


def test_fetch_enforces_redirect_limit() -> None:
    def request(url: str, _timeout: float, _max_bytes: int) -> HttpResponse:
        return HttpResponse(302, {"location": url + "/next"}, b"")

    result = fetch_html(
        "https://example.org",
        request_once=request,
        resolver=lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
        max_redirects=2,
    )

    assert result.status == "fetch_error"
    assert result.message == "redirect_limit"


def test_fetch_returns_full_bounded_html() -> None:
    body = b"<html><body>Hello</body></html>"
    result = fetch_html(
        "example.org",
        request_once=lambda *_args: HttpResponse(200, {"content-type": "text/html"}, body),
        resolver=lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    assert result.status == "ok"
    assert result.body == body
    assert result.final_url == "https://example.org"


def test_fetch_classifies_request_exception_without_leaking_details() -> None:
    def request(*_args):
        raise TimeoutError("secret internal endpoint")

    result = fetch_html(
        "https://example.org",
        request_once=request,
        resolver=lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    assert result.status == "fetch_error"
    assert result.message == "TimeoutError"
