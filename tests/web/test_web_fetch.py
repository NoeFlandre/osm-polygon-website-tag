"""Hermetic tests for bounded and SSRF-safe website downloads."""

from __future__ import annotations

import io
import socket
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from typing import TypedDict, Unpack

import pytest

import osm_polygon_website_tag.web.web_fetch as web_fetch_module
from osm_polygon_website_tag import __version__
from osm_polygon_website_tag.web.politeness import HostLimiter, HostPolicy
from osm_polygon_website_tag.web.web_fetch import (
    FetchResult,
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
    assert result.charset is None


@pytest.mark.parametrize(
    ("content_type", "charset"),
    [
        ("text/html; charset=Shift_JIS", "Shift_JIS"),
        ('text/html; charset="windows-1252"', "windows-1252"),
        ("text/html; charset=", None),
        ("text/html", None),
        ('TEXT/HTML;CHARSET = "koi8-r" ; x=1', "koi8-r"),
        ('text/html; note="charset=koi8-r"; charset=windows-1252', "windows-1252"),
        ('text/html; note="a \\" ; charset=koi8-r"; charset=utf-8', "utf-8"),
        ("text/html; xcharset=koi8-r", None),
        ("text/html; charset=koi8-r; charset=utf-8", "koi8-r"),
        ('text/html; charset=""', None),
    ],
)
def test_fetch_reports_header_charset(content_type: str, charset: str | None) -> None:
    result = fetch_html(
        "example.org",
        request_once=lambda *_args: HttpResponse(200, {"Content-Type": content_type}, b"<p>x</p>"),
        resolver=lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    assert result.charset == charset


def test_header_charset_ignores_missing_header() -> None:
    assert web_fetch_module._header_charset({}) is None


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


def test_private_fetch_classifiers_and_redirect_helpers_are_deterministic() -> None:
    response = HttpResponse(200, {"Content-Type": "text/html"}, b"body")
    assert web_fetch_module._header(response.headers, "content-type") == "text/html"
    assert web_fetch_module._header(response.headers, "missing") is None
    assert web_fetch_module._response_error(response, 10) is None
    assert web_fetch_module._response_error(response, 3) == "response_too_large"
    assert (
        web_fetch_module._response_error(HttpResponse(200, {"Content-Type": "image/png"}, b"x"), 10)
        == "unsupported_content_type"
    )
    next_url, terminal = web_fetch_module._redirect_step(
        HttpResponse(302, {"location": "/next"}, b""),
        "https://example.org",
        "requested",
        0,
        2,
    )
    assert next_url == "https://example.org/next"
    assert terminal is None
    _, no_location = web_fetch_module._redirect_step(
        HttpResponse(302, {}, b""), "https://example.org", "requested", 0, 2
    )
    assert isinstance(no_location, FetchResult)
    assert no_location.message == "redirect_without_location"
    _, limit = web_fetch_module._redirect_step(
        HttpResponse(302, {"location": "/next"}, b""),
        "https://example.org",
        "requested",
        2,
        2,
    )
    assert isinstance(limit, FetchResult)
    assert limit.message == "redirect_limit"
    assert (
        web_fetch_module._terminal_response(response, "https://example.org", "requested", 10).status
        == "ok"
    )

    def resolver(_host: str, _port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    assert (
        web_fetch_module._safe_request(
            "https://example.org",
            "requested",
            lambda *_args: response,
            resolver,
            1.0,
            10,
        )
        == response
    )
    failed = web_fetch_module._safe_request(
        "https://example.org",
        "requested",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("hidden")),
        resolver,
        1.0,
        10,
    )
    assert isinstance(failed, FetchResult)
    assert failed.status == "fetch_error"


def test_fetch_classifies_transport_unsafe_error_as_unsafe_url() -> None:
    def request(*_args):
        raise UnsafeUrlError("non_global_address")

    result = fetch_html(
        "https://example.org",
        request_once=request,
        resolver=lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    assert result == FetchResult(
        "unsafe_url", "https://example.org", final_url="https://example.org", message="unsafe_url"
    )


class _FakeSocket:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.closed = False

    def getpeername(self) -> tuple[str, int]:
        return (self.peer, 443)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("peer", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"])
def test_connect_rejects_rebound_private_peer(monkeypatch, peer: str) -> None:
    sock = _FakeSocket(peer)
    monkeypatch.setattr(web_fetch_module.socket, "create_connection", lambda *_a, **_k: sock)

    with pytest.raises(UnsafeUrlError, match=r"^non_global_address$"):
        web_fetch_module._connect_public(("example.org", 443), 3.0)

    assert sock.closed


@pytest.mark.parametrize(
    ("connection", "handler", "method"),
    [
        ("_PublicHTTPConnection", "_PublicHTTPHandler", "http_open"),
        ("_PublicHTTPSConnection", "_PublicHTTPSHandler", "https_open"),
    ],
)
def test_handlers_open_peer_checked_connections(connection: str, handler: str, method: str) -> None:
    conn = getattr(web_fetch_module, connection)("example.org")
    assert conn._create_connection is web_fetch_module._connect_public
    seen: list[tuple[object, object]] = []
    opener = getattr(web_fetch_module, handler)()
    opener.do_open = lambda cls, req: seen.append((cls, req)) or "response"

    assert getattr(opener, method)("req") == "response"
    assert seen == [(getattr(web_fetch_module, connection), "req")]


# --- Behaviour pins: every branch below is observable and mutation-gated. ---

_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _public_resolver(_host: str, _port: int):
    return _PUBLIC


def test_module_constants_are_pinned() -> None:
    assert web_fetch_module.MAX_RESPONSE_BYTES == 20_000_000
    assert web_fetch_module.REQUEST_TIMEOUT_SECONDS == 30.0
    assert web_fetch_module.MAX_REDIRECTS == 3
    assert web_fetch_module.READ_CHUNK_BYTES == 65_536
    assert (
        f"osm-polygon-website-tag/{__version__} (+https://github.com/NoeFlandre/osm-polygon-website-tag)"
    ) == web_fetch_module.USER_AGENT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  example.org  ", "https://example.org"),
        ("example.org", "https://example.org"),
        ("https://example.org:443/a?q=1#frag", "https://example.org/a?q=1"),
        ("http://example.org:80/", "http://example.org/"),
        ("http://example.org:443/", "http://example.org:443/"),
        ("https://example.org:80/", "https://example.org:80/"),
        ("https://example.org:8443/x", "https://example.org:8443/x"),
        ("HTTPS://EXAMPLE.org./", "https://example.org/"),
        ("https://[2606:2800:220:1::1]/", "https://[2606:2800:220:1::1]/"),
        ("https://[2606:2800:220:1::1]:8080/", "https://[2606:2800:220:1::1]:8080/"),
        ("http://[2606:2800:220:1::1]:80/", "http://[2606:2800:220:1::1]/"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("example.org/a:b", "https://example.org/a:b"),
        ("//Example.org:8080/p", "https://example.org:8080/p"),
        ("mylocalhost.org", "https://mylocalhost.org"),
        ("localhost.org", "https://localhost.org"),
    ],
)
def test_normalize_http_url_exact(raw: str, expected: str) -> None:
    assert normalize_http_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "empty_url"),
        ("   ", "empty_url"),
        ("ftp://example.org", "unsupported_scheme"),
        ("mailto:test@example.org", "unsupported_scheme"),
        ("example.org:8080/x", "unsupported_scheme"),
        ("https://user:password@example.org", "credentials_not_allowed"),
        ("https://user@example.org", "credentials_not_allowed"),
        ("https://:password@example.org", "credentials_not_allowed"),
        ("https:///path", "missing_hostname"),
        ("https://localhost/", "localhost_not_allowed"),
        ("https://LOCALHOST./", "localhost_not_allowed"),
        ("https://api.localhost/", "localhost_not_allowed"),
        ("https://" + "a" * 64 + ".org/", "invalid_hostname"),
    ],
)
def test_normalize_rejection_messages(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        normalize_http_url(raw)


def test_encode_hostname_wraps_unicode_error() -> None:
    with pytest.raises(ValueError, match=r"^invalid_hostname$") as info:
        web_fetch_module._encode_hostname("a" * 64)
    assert isinstance(info.value.__cause__, UnicodeError)
    assert not isinstance(info.value, UnicodeError)


@pytest.mark.parametrize(
    ("url", "host", "port"),
    [
        ("https://example.org", "example.org", 443),
        ("http://example.org", "example.org", 80),
        ("https://example.org:8443", "example.org", 8443),
        ("http://example.org:8080", "example.org", 8080),
    ],
)
def test_validate_resolves_host_and_port(url: str, host: str, port: int) -> None:
    calls = []

    def resolve(h: str, p: int):
        calls.append((h, p))
        return _PUBLIC

    assert validate_public_http_url(url, resolver=resolve) is True
    assert calls == [(host, port)]


@pytest.mark.parametrize(
    "literal",
    [
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://10.1.2.3/",
        "https://0.0.0.0/",
        "https://224.0.0.1/",
        "https://240.0.0.1/",
        "https://[::]/",
    ],
)
def test_validate_rejects_private_literal_before_resolving(literal: str) -> None:
    calls = []

    def resolve(*args):
        calls.append(args)
        return _PUBLIC

    with pytest.raises(UnsafeUrlError, match=r"^non_global_address$"):
        validate_public_http_url(literal, resolver=resolve)
    assert calls == []


def test_validate_public_literal_uses_default_resolver() -> None:
    assert validate_public_http_url("https://93.184.216.34/") is True
    assert validate_public_http_url("https://[2606:2800:220:1::1]/") is True


def test_fetch_default_resolver_and_limits() -> None:
    calls = []

    def request(url: str, timeout: float, max_bytes: int) -> HttpResponse:
        calls.append((url, timeout, max_bytes))
        return HttpResponse(302, {"Location": url + "x"}, b"")

    result = fetch_html("https://93.184.216.34/", request_once=request)
    assert result == FetchResult(
        "fetch_error",
        "https://93.184.216.34/",
        final_url="https://93.184.216.34/xxx",
        message="redirect_limit",
    )
    assert calls == [
        ("https://93.184.216.34/", 30.0, 20_000_000),
        ("https://93.184.216.34/x", 30.0, 20_000_000),
        ("https://93.184.216.34/xx", 30.0, 20_000_000),
        ("https://93.184.216.34/xxx", 30.0, 20_000_000),
    ]


@pytest.mark.parametrize(
    ("resolver", "message"),
    [
        (lambda *_a: (_ for _ in ()).throw(OSError("nx")), "dns_resolution_failed"),
        (lambda *_a: [], "dns_resolution_empty"),
        (
            lambda *_a: [*_PUBLIC, (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
            "non_global_address",
        ),
    ],
)
def test_validate_resolution_failures(resolver, message: str) -> None:
    with pytest.raises(UnsafeUrlError, match=f"^{message}$"):
        validate_public_http_url("https://example.org", resolver=resolver)


def test_resolve_failure_chains_os_error() -> None:
    err = OSError("nx")

    def resolve(*_a):
        raise err

    with pytest.raises(UnsafeUrlError) as info:
        web_fetch_module._resolve_addresses(resolve, "example.org", 443)
    assert info.value.__cause__ is err
    assert web_fetch_module._resolve_addresses(_public_resolver, "h", 1) == _PUBLIC


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:2800:220:1::1", True),
        ("10.0.0.1", False),
        ("0.0.0.0", False),  # noqa: S104
        ("240.0.0.1", False),
        ("::", False),
        ("ff0e::1", False),
        ("224.0.0.1", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    import ipaddress

    assert web_fetch_module._is_public_address(ipaddress.ip_address(address)) is public


def test_fetch_invalid_initial_url_result() -> None:
    def never_called(*_args: object) -> HttpResponse:
        raise AssertionError("an invalid URL must not be requested")

    assert fetch_html("ftp://x", request_once=never_called) == FetchResult(
        "invalid_url", "ftp://x", message="invalid_url"
    )
    assert fetch_html("https://" + "a" * 64 + ".org") == FetchResult(
        "invalid_url", "https://" + "a" * 64 + ".org", message="invalid_url"
    )


def test_normalise_requested_url_catches_unicode_error(monkeypatch) -> None:
    def boom(_raw):
        raise UnicodeError("x")

    monkeypatch.setattr(web_fetch_module, "normalize_http_url", boom)
    assert web_fetch_module._normalise_requested_url("x") == FetchResult(
        "invalid_url", "x", message="invalid_url"
    )


def test_fetch_unsafe_initial_resolution_result() -> None:
    def request(*_a):
        raise AssertionError("must not request")

    assert fetch_html(
        "https://example.org/p", request_once=request, resolver=lambda *_a: []
    ) == FetchResult(
        "unsafe_url",
        "https://example.org/p",
        final_url="https://example.org/p",
        message="unsafe_url",
    )


def test_fetch_unsafe_redirect_exact_result() -> None:
    result = fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(301, {"LOCATION": "http://127.0.0.1/x"}, b""),
        resolver=_public_resolver,
    )
    assert result == FetchResult(
        "unsafe_url", "https://example.org", final_url="http://127.0.0.1/x", message="unsafe_url"
    )


def test_fetch_error_result_exact() -> None:
    def request(*_a):
        raise TimeoutError("secret")

    assert fetch_html(
        "https://example.org", request_once=request, resolver=_public_resolver
    ) == FetchResult(
        "fetch_error",
        "https://example.org",
        final_url="https://example.org",
        message="TimeoutError",
    )


@pytest.mark.parametrize("max_redirects", [0, 1, 2, 3])
def test_redirect_limit_boundary(max_redirects: int) -> None:
    urls = []

    def request(url: str, *_a) -> HttpResponse:
        urls.append(url)
        n = len(urls)
        if n <= max_redirects:
            return HttpResponse(302, {"location": f"/r{n}"}, b"")
        return HttpResponse(200, {"content-type": "text/html"}, b"done")

    result = fetch_html(
        "https://example.org",
        request_once=request,
        resolver=_public_resolver,
        max_redirects=max_redirects,
    )
    final = f"https://example.org/r{max_redirects}" if max_redirects else "https://example.org"
    assert result == FetchResult(
        "ok", "https://example.org", final_url=final, body=b"done", media_type="text/html"
    )
    assert len(urls) == max_redirects + 1


def test_redirect_limit_exceeded_exact() -> None:
    urls = []

    def request(url: str, *_a) -> HttpResponse:
        urls.append(url)
        return HttpResponse(307, {"location": f"/r{len(urls)}"}, b"")

    result = fetch_html(
        "https://example.org", request_once=request, resolver=_public_resolver, max_redirects=1
    )
    assert result == FetchResult(
        "fetch_error",
        "https://example.org",
        final_url="https://example.org/r1",
        message="redirect_limit",
    )
    assert urls == ["https://example.org", "https://example.org/r1"]


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308, 399])
def test_redirect_statuses_follow_location(status: int) -> None:
    seen = []

    def request(url: str, *_a) -> HttpResponse:
        seen.append(url)
        if len(seen) == 1:
            return HttpResponse(status, {"location": "/n"}, b"")
        return HttpResponse(200, {"Content-Type": "text/html"}, b"b")

    result = fetch_html("https://example.org", request_once=request, resolver=_public_resolver)
    assert result == FetchResult(
        "ok",
        "https://example.org",
        "https://example.org/n",
        b"b",
        media_type="text/html",
    )


def test_redirect_without_location_exact() -> None:
    assert fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(302, {"x": "y"}, b""),
        resolver=_public_resolver,
    ) == FetchResult(
        "fetch_error",
        "https://example.org",
        final_url="https://example.org",
        message="redirect_without_location",
    )


def test_redirect_to_invalid_url_exact() -> None:
    assert fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(302, {"location": "ftp://evil/"}, b""),
        resolver=_public_resolver,
    ) == FetchResult(
        "invalid_url",
        "https://example.org",
        final_url="https://example.org",
        message="invalid_redirect",
    )


def test_redirect_step_results_exact() -> None:
    step = web_fetch_module._redirect_step
    assert step(HttpResponse(302, {"Location": "b?q"}, b""), "https://e.org/a/", "req", 0, 1) == (
        "https://e.org/a/b?q",
        None,
    )
    assert step(HttpResponse(302, {"location": "/n"}, b""), "https://e.org", "req", 1, 1) == (
        None,
        FetchResult("fetch_error", "req", final_url="https://e.org", message="redirect_limit"),
    )
    assert step(HttpResponse(302, {}, b""), "https://e.org", "req", 1, 1) == (
        None,
        FetchResult(
            "fetch_error", "req", final_url="https://e.org", message="redirect_without_location"
        ),
    )


@pytest.mark.parametrize("status", [100, 199, 300, 304, 399, 400, 404, 500])
def test_status_error_non_2xx(status: int) -> None:
    assert web_fetch_module._response_error(HttpResponse(status, {}, b""), 0) == f"http_{status}"


@pytest.mark.parametrize("status", [200, 204, 299])
def test_status_ok_2xx(status: int) -> None:
    headers = {"Content-Type": "text/html"}
    assert web_fetch_module._response_error(HttpResponse(status, headers, b""), 0) is None
    assert fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(status, headers, b"x"),
        resolver=_public_resolver,
    ) == FetchResult(
        "ok", "https://example.org", "https://example.org", b"x", media_type="text/html"
    )


@pytest.mark.parametrize("status", [199, 404, 500])
def test_fetch_http_error_exact(status: int) -> None:
    assert fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(status, {"content-type": "image/png"}, b"x" * 99),
        resolver=_public_resolver,
        max_bytes=1,
    ) == FetchResult(
        "fetch_error",
        "https://example.org",
        final_url="https://example.org",
        message=f"http_{status}",
    )


def test_size_limit_boundary() -> None:
    size = web_fetch_module._response_error
    headers = {"Content-Type": "text/html"}
    assert size(HttpResponse(200, headers, b"abc"), 3) is None
    assert size(HttpResponse(200, headers, b"abcd"), 3) == "response_too_large"
    assert fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(200, {"content-type": "image/png"}, b"abcd"),
        resolver=_public_resolver,
        max_bytes=3,
    ) == FetchResult(
        "fetch_error",
        "https://example.org",
        final_url="https://example.org",
        message="response_too_large",
    )
    assert fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(200, headers, b"abc"),
        resolver=_public_resolver,
        max_bytes=3,
    ) == FetchResult(
        "ok", "https://example.org", "https://example.org", b"abc", media_type="text/html"
    )


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "TEXT/HTML; charset=utf-8",
        "application/xhtml+xml",
        "Application/XHTML+XML; charset=utf-8",
        "text/plain",
        "text/plain; charset=latin-1",
    ],
)
def test_content_type_allowed(content_type: str) -> None:
    response = HttpResponse(200, {"Content-Type": content_type}, b"x")
    assert web_fetch_module._response_error(response, 10) is None
    assert fetch_html(
        "https://example.org", request_once=lambda *_a: response, resolver=_public_resolver
    ) == FetchResult(
        "ok",
        "https://example.org",
        "https://example.org",
        b"x",
        charset=web_fetch_module._header_charset(response.headers),
        media_type=web_fetch_module._header_media_type(response.headers),
    )


def test_a_missing_or_empty_content_type_is_rejected() -> None:
    assert web_fetch_module._response_error(HttpResponse(200, {}, b"x"), 10) == (
        "unsupported_content_type"
    )
    assert (
        web_fetch_module._response_error(HttpResponse(200, {"Content-Type": ""}, b"x"), 10)
        == "unsupported_content_type"
    )


@pytest.mark.parametrize(
    "content_type", ["image/png", "application/json", "text/css", "application/xml", "XX"]
)
def test_content_type_rejected(content_type: str) -> None:
    response = HttpResponse(200, {"CONTENT-TYPE": content_type}, b"x")
    assert web_fetch_module._response_error(response, 10) == "unsupported_content_type"
    assert fetch_html(
        "https://example.org", request_once=lambda *_a: response, resolver=_public_resolver
    ) == FetchResult(
        "fetch_error",
        "https://example.org",
        final_url="https://example.org",
        message="unsupported_content_type",
    )


def test_header_is_case_insensitive_and_returns_first_match() -> None:
    headers = {"X-A": "1", "Content-TYPE": "text/html", "content-type": "other"}
    assert web_fetch_module._header(headers, "CONTENT-type") == "text/html"
    assert web_fetch_module._header(headers, "x-a") == "1"
    assert web_fetch_module._header({}, "x") is None
    assert web_fetch_module._header({"a": ""}, "A") == ""


def test_terminal_response_exact() -> None:
    assert web_fetch_module._terminal_response(
        HttpResponse(200, {"Content-Type": "text/html"}, b"z"), "cur", "req", 1
    ) == FetchResult("ok", "req", final_url="cur", body=b"z", media_type="text/html")


def test_safe_request_passes_arguments_and_classifies() -> None:
    calls = []
    resolved = []

    def resolver(host, port):
        resolved.append((host, port))
        return _PUBLIC

    def transport(url, timeout, max_bytes):
        calls.append((url, timeout, max_bytes))
        return HttpResponse(200, {}, b"")

    web_fetch_module._safe_request("https://e.org:81/", "req", transport, resolver, 1.5, 7)
    assert calls == [("https://e.org:81/", 1.5, 7)]
    assert resolved == [("e.org", 81)]

    def unsafe(*_a):
        raise UnsafeUrlError("x")

    assert web_fetch_module._safe_request(
        "https://e.org/", "req", unsafe, resolver, 1.0, 1
    ) == FetchResult("unsafe_url", "req", final_url="https://e.org/", message="unsafe_url")
    assert web_fetch_module._safe_request(
        "https://e.org/", "req", transport, lambda *_a: [], 1.0, 1
    ) == FetchResult("unsafe_url", "req", final_url="https://e.org/", message="unsafe_url")

    def broken(*_a):
        raise KeyError("k")

    assert web_fetch_module._safe_request(
        "https://e.org/", "req", broken, resolver, 1.0, 1
    ) == FetchResult("fetch_error", "req", final_url="https://e.org/", message="KeyError")


def test_no_redirect_handler_returns_none() -> None:
    assert (
        web_fetch_module._NoRedirect().redirect_request(
            web_fetch_module.urllib.request.Request("http://e.org"),
            None,
            302,
            "Found",
            {},
            "http://x",
        )
        is None
    )


def test_public_connections_keep_constructor_arguments() -> None:
    http_conn = web_fetch_module._PublicHTTPConnection("example.org", 8080, timeout=2.0)
    assert (http_conn.host, http_conn.port, http_conn.timeout) == ("example.org", 8080, 2.0)
    https_conn = web_fetch_module._PublicHTTPSConnection("example.org", 8443, timeout=3.0)
    assert (https_conn.host, https_conn.port, https_conn.timeout) == ("example.org", 8443, 3.0)
    assert https_conn._create_connection is web_fetch_module._connect_public


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//example.org/a", "https://example.org/a"),
        ("example.org/a", "https://example.org/a"),
        ("example.org/a:b/c", "https://example.org/a:b/c"),
        ("http://example.org", "http://example.org"),
    ],
)
def test_coerce_http_value_adds_a_lowercase_https_scheme(raw: str, expected: str) -> None:
    assert web_fetch_module._coerce_http_value(raw) == expected


def test_coerce_http_value_rejects_a_colon_only_in_the_host_part() -> None:
    with pytest.raises(ValueError, match=r"^unsupported_scheme$"):
        web_fetch_module._coerce_http_value("mailto:someone/path")


def test_fetch_classifies_http_400_as_a_status_error_not_a_redirect() -> None:
    result = fetch_html(
        "https://example.org",
        request_once=lambda *_args: HttpResponse(400, {}, b""),
        resolver=lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    assert result == FetchResult(
        "fetch_error", "https://example.org", final_url="https://example.org", message="http_400"
    )


class _TrickleResponse:
    def __init__(self, payload: bytes) -> None:
        self.pending = payload
        self.limits: list[int] = []

    def read1(self, limit: int) -> bytes:
        self.limits.append(limit)
        chunk, self.pending = self.pending[:limit], self.pending[limit:]
        return chunk


def test_read_before_deadline_reads_in_bounded_chunks(monkeypatch) -> None:
    monkeypatch.setattr(web_fetch_module, "READ_CHUNK_BYTES", 4)
    response = _TrickleResponse(b"abcdefghij")

    body = web_fetch_module._read_before_deadline(response, 9, deadline=float("inf"))

    assert body == b"abcdefghi"
    assert response.limits == [4, 4, 1]


class _CountingResponse:
    """A urllib response that counts the body bytes read from it."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.pending = body
        self.bytes_read = 0

    def __enter__(self) -> _CountingResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read1(self, limit: int) -> bytes:
        chunk, self.pending = self.pending[:limit], self.pending[limit:]
        self.bytes_read += len(chunk)
        return chunk


def _download(
    monkeypatch: pytest.MonkeyPatch, response: _CountingResponse, max_bytes: int
) -> HttpResponse:
    class Opener:
        def open(self, _request: object, *, timeout: float) -> _CountingResponse:
            return response

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_a: Opener())
    return web_fetch_module._download_once("https://example.org", 3.0, max_bytes)


def test_download_once_keeps_an_http_error_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingBody(io.BytesIO):
        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.bytes_read = 0

        def read1(self, size: int | None = -1) -> bytes:
            data = super().read1(size)
            self.bytes_read += len(data)
            return data

    body = CountingBody(b"not found")
    headers = Message()
    headers["Content-Type"] = "text/html"
    response = urllib.error.HTTPError("https://example.org", 404, "Not Found", headers, body)

    class Opener:
        def open(self, _request: object, *, timeout: float) -> urllib.error.HTTPError:
            raise response

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_a: Opener())

    result = web_fetch_module._download_once("https://example.org", 3.0, 100)

    assert (result.status_code, result.body) == (404, b"")
    assert body.bytes_read == 0


def test_download_once_rethrows_a_non_url_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.URLError("connection failed")

    class Opener:
        def open(self, _request: object, *, timeout: float) -> object:
            raise error

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_a: Opener())

    with pytest.raises(urllib.error.URLError) as caught:
        web_fetch_module._download_once("https://example.org", 3.0, 100)

    assert caught.value is error


def test_download_once_rethrows_an_unsafe_url_from_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = UnsafeUrlError("unsafe peer")

    class Opener:
        def open(self, _request: object, *, timeout: float) -> object:
            raise urllib.error.URLError(reason)

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_a: Opener())

    with pytest.raises(UnsafeUrlError) as caught:
        web_fetch_module._download_once("https://example.org", 3.0, 100)

    assert caught.value is reason


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (404, {"Content-Type": "text/html"}),
        (500, {}),
        (302, {"Location": "https://example.org/next"}),
        (200, {}),
        (200, {"Content-Type": "image/png"}),
        (200, {"Content-Type": "application/pdf; charset=binary"}),
        (200, {"Content-Type": "text/html", "Content-Length": "50000000"}),
    ],
)
def test_a_page_refused_by_its_headers_reads_no_body(
    monkeypatch: pytest.MonkeyPatch, status: int, headers: dict[str, str]
) -> None:
    response = _CountingResponse(status, headers, b"x" * 1000)

    result = _download(monkeypatch, response, 100)

    assert response.bytes_read == 0
    assert (result.status_code, result.body) == (status, b"")
    assert result.headers == headers


@pytest.mark.parametrize(
    "headers",
    [{"Content-Type": "text/html; charset=utf-8", "Content-Length": "500"}],
)
def test_a_usable_page_is_still_read_in_full(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    response = _CountingResponse(200, headers, b"x" * 500)

    assert _download(monkeypatch, response, 1000).body == b"x" * 500
    assert response.bytes_read == 500


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Content-Type": "text/html", "Content-Length": "101"}, "response_too_large"),
        ({"Content-Type": "text/html", "content-length": " 101 "}, "response_too_large"),
        ({"Content-Type": "text/html", "Content-Length": "100"}, None),
        ({"Content-Type": "text/html", "Content-Length": "-5"}, None),
        ({"Content-Type": "text/html", "Content-Length": "abc"}, None),
        ({"Content-Type": "text/html", "Content-Length": ""}, None),
        ({"Content-Type": "text/html"}, None),
    ],
)
def test_declared_length_over_the_limit_is_refused(
    headers: dict[str, str], expected: str | None
) -> None:
    assert web_fetch_module._response_error(HttpResponse(200, headers, b""), 100) == expected


@pytest.mark.parametrize("content_type", ["text/htmlx", "xtext/html", "text/html-fragment"])
def test_the_media_type_must_match_exactly(content_type: str) -> None:
    response = HttpResponse(200, {"Content-Type": content_type}, b"x")

    assert web_fetch_module._response_error(response, 10) == "unsupported_content_type"


def test_status_outranks_size_and_size_outranks_type() -> None:
    error = web_fetch_module._response_error
    headers = {"Content-Type": "image/png", "Content-Length": "500"}

    assert error(HttpResponse(404, headers, b""), 10) == "http_404"
    assert error(HttpResponse(200, headers, b""), 10) == "response_too_large"
    assert error(HttpResponse(200, {"Content-Type": "image/png"}, b""), 10) == (
        "unsupported_content_type"
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _HostPolicyOptions(TypedDict, total=False):
    concurrency: int
    delay_seconds: float
    max_retry_after_seconds: float


def _polite_fetch(
    responses: list[HttpResponse],
    url: str = "https://example.org/a",
    **policy: Unpack[_HostPolicyOptions],
) -> tuple[FetchResult, list[str], _FakeClock]:
    clock = _FakeClock()
    limiter = HostLimiter(HostPolicy(**policy), clock=clock, sleep=clock.sleep)
    calls: list[str] = []

    def transport(target: str, _timeout: float, _max_bytes: int) -> HttpResponse:
        calls.append(target)
        return responses[len(calls) - 1]

    result = fetch_html(url, request_once=transport, resolver=_public_resolver, limiter=limiter)
    return result, calls, clock


@pytest.mark.parametrize("status", [429, 503])
def test_a_short_retry_after_is_waited_out_then_retried_once(status: int) -> None:
    result, calls, clock = _polite_fetch(
        [
            HttpResponse(status, {"Retry-After": "2"}, b""),
            HttpResponse(200, {"Content-Type": "text/html"}, b"<p>ok</p>"),
        ],
        concurrency=1,
        delay_seconds=0.0,
    )

    assert result.status == "ok"
    assert result.body == b"<p>ok</p>"
    assert calls == ["https://example.org/a"] * 2
    assert clock.sleeps == [2.0]


def test_retry_after_is_recorded_before_releasing_the_host_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    limiter = HostLimiter(
        HostPolicy(concurrency=1, delay_seconds=0), clock=clock, sleep=clock.sleep
    )
    original_slot = limiter.slot
    original_back_off = limiter.back_off
    slot_held = False
    back_off_states: list[bool] = []
    responses = [
        HttpResponse(429, {"Retry-After": "2"}, b""),
        HttpResponse(200, {"Content-Type": "text/html"}, b"ok"),
    ]
    calls = 0

    @contextmanager
    def tracked_slot(host: str) -> Iterator[None]:
        nonlocal slot_held
        with original_slot(host):
            slot_held = True
            try:
                yield
            finally:
                slot_held = False

    def tracked_back_off(host: str, seconds: float) -> None:
        back_off_states.append(slot_held)
        original_back_off(host, seconds)

    def transport(_url: str, _timeout: float, _max_bytes: int) -> HttpResponse:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    monkeypatch.setattr(limiter, "slot", tracked_slot)
    monkeypatch.setattr(limiter, "back_off", tracked_back_off)

    result = fetch_html(
        "https://example.org/a",
        request_once=transport,
        resolver=_public_resolver,
        limiter=limiter,
    )

    assert result.status == "ok"
    assert back_off_states == [True]
    assert clock.sleeps == [2.0]


def test_only_one_retry_is_made() -> None:
    busy = HttpResponse(429, {"Retry-After": "1"}, b"")

    result, calls, _clock = _polite_fetch([busy, busy], concurrency=1, delay_seconds=0.0)

    assert (result.status, result.message) == ("fetch_error", "http_429")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(429, {"Retry-After": "31"}, b""),
        HttpResponse(429, {}, b""),
        HttpResponse(429, {"Retry-After": "later"}, b""),
        HttpResponse(404, {"Retry-After": "1"}, b""),
        HttpResponse(500, {"Retry-After": "1"}, b""),
    ],
)
def test_other_failures_are_not_retried(response: HttpResponse) -> None:
    result, calls, _clock = _polite_fetch([response], concurrency=1, delay_seconds=0.0)

    assert result.message == f"http_{response.status_code}"
    assert len(calls) == 1


def test_a_retry_after_at_the_cap_is_still_honoured() -> None:
    _result, calls, clock = _polite_fetch(
        [
            HttpResponse(429, {"Retry-After": "30"}, b""),
            HttpResponse(200, {"Content-Type": "text/html"}, b"x"),
        ],
        concurrency=1,
        delay_seconds=0.0,
    )

    assert len(calls) == 2
    assert clock.sleeps == [30.0]


def test_the_retry_after_cap_is_configurable() -> None:
    _result, calls, _clock = _polite_fetch(
        [HttpResponse(429, {"Retry-After": "5"}, b"")],
        concurrency=1,
        delay_seconds=0.0,
        max_retry_after_seconds=4.0,
    )

    assert len(calls) == 1


def test_a_polite_fetch_spaces_two_requests_to_one_host() -> None:
    clock = _FakeClock()
    limiter = HostLimiter(
        HostPolicy(concurrency=1, delay_seconds=1.5), clock=clock, sleep=clock.sleep
    )

    for path in ("a", "b"):
        fetch_html(
            f"https://example.org/{path}",
            request_once=lambda *_a: HttpResponse(200, {"Content-Type": "text/html"}, b"x"),
            resolver=_public_resolver,
            limiter=limiter,
        )

    assert clock.sleeps == [1.5]


def test_the_limiter_is_keyed_by_the_requested_host() -> None:
    clock = _FakeClock()
    limiter = HostLimiter(
        HostPolicy(concurrency=1, delay_seconds=9.0), clock=clock, sleep=clock.sleep
    )

    for host in ("a.example", "b.example"):
        fetch_html(
            f"https://{host}/",
            request_once=lambda *_a: HttpResponse(200, {"Content-Type": "text/html"}, b"x"),
            resolver=_public_resolver,
            limiter=limiter,
        )

    assert clock.sleeps == []


def test_without_a_limiter_the_transport_is_called_directly() -> None:
    seen: list[str] = []

    fetch_html(
        "https://example.org/",
        request_once=lambda url, *_a: (
            seen.append(url) or HttpResponse(200, {"Content-Type": "text/html"}, b"x")
        ),
        resolver=_public_resolver,
    )

    assert seen == ["https://example.org/"]


@pytest.mark.parametrize("raw", [".", "..", "https://.", "http://...:8080/x", "//."])
def test_a_host_made_only_of_dots_is_missing_not_empty(raw: str) -> None:
    with pytest.raises(ValueError, match=r"^missing_hostname$"):
        web_fetch_module.normalize_http_url(raw)


def test_connect_public_forwards_positional_and_keyword_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []
    sock = _FakeSocket("93.184.216.34")
    monkeypatch.setattr(
        web_fetch_module.socket,
        "create_connection",
        lambda *args, **kwargs: seen.append((args, kwargs)) or sock,
    )

    assert web_fetch_module._connect_public(("h", 443), 3.0, ("0.0.0.0", 0)) is sock  # noqa: S104
    assert web_fetch_module._connect_public(("h", 443), timeout=2.0) is sock

    assert seen == [
        ((("h", 443), 3.0, ("0.0.0.0", 0)), {}),  # noqa: S104
        ((("h", 443),), {"timeout": 2.0}),
    ]
    assert not sock.closed


class _Ticks:
    """A clock that returns each of its values once, then the last one for ever."""

    def __init__(self, *values: float) -> None:
        self.values = list(values)

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class _Trickle:
    """A response that hands back one byte per read, however much is asked for."""

    def __init__(self, body: bytes) -> None:
        self.pending = body

    def read1(self, limit: int) -> bytes:
        chunk, self.pending = self.pending[:1], self.pending[1:]
        return chunk


def test_the_deadline_is_reached_when_the_clock_equals_it() -> None:
    with pytest.raises(TimeoutError, match=r"^request deadline exceeded$"):
        web_fetch_module._read_before_deadline(_Trickle(b"abc"), 10, 5.0, clock=_Ticks(5.0))


def test_a_read_just_inside_the_deadline_completes() -> None:
    body = web_fetch_module._read_before_deadline(
        _Trickle(b"abc"), 10, 5.0, clock=_Ticks(4.999, 4.999)
    )

    assert body == b"abc"


def test_the_deadline_is_checked_between_every_read() -> None:
    with pytest.raises(TimeoutError):
        web_fetch_module._read_before_deadline(
            _Trickle(b"abcdef"), 6, 5.0, clock=_Ticks(1.0, 2.0, 5.0)
        )


class _Head:
    def __init__(self, status: int | None, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> _Head:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read1(self, limit: int) -> bytes:
        return b""


def test_download_once_builds_a_direct_opener_and_a_named_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Opener:
        def open(self, request: object, *, timeout: float) -> _Head:
            seen["request"], seen["timeout"] = request, timeout
            return _Head(200, {"Content-Type": "text/html"})

    def build_opener(*handlers: object) -> Opener:
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(web_fetch_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        web_fetch_module,
        "_read_before_deadline",
        lambda _response, limit, deadline: seen.update(limit=limit, deadline=deadline) or b"",
    )

    web_fetch_module._download_once("http://example.org/p", 4.0, 10)

    handlers = seen["handlers"]
    assert [type(h) for h in handlers] == [  # ty: ignore[not-iterable]
        web_fetch_module.urllib.request.ProxyHandler,
        web_fetch_module._NoRedirect,
        web_fetch_module._PublicHTTPHandler,
        web_fetch_module._PublicHTTPSHandler,
    ]
    assert handlers[0].proxies == {}  # ty: ignore[not-subscriptable]
    assert seen["timeout"] == 4.0
    assert (seen["limit"], seen["deadline"]) == (11, 104.0)  # deadline counts from before connect
    request = seen["request"]
    assert request.get_header("User-agent") == web_fetch_module.USER_AGENT  # ty: ignore[unresolved-attribute]
    assert request.full_url == "http://example.org/p"  # ty: ignore[unresolved-attribute]


def test_a_response_without_a_status_is_reported_as_status_zero() -> None:
    head = web_fetch_module._response_head(_Head(None, {"A": "b"}))

    assert head == HttpResponse(0, {"A": "b"}, b"")


@pytest.mark.parametrize(
    ("status", "headers", "worth"),
    [
        (199, {}, False),
        (200, {"Content-Type": "text/html"}, True),
        (200, {}, False),
        (299, {"Content-Type": "text/html"}, True),
        (300, {"Content-Type": "text/html"}, False),
        (301, {"Content-Type": "text/html"}, False),
        (404, {"Content-Type": "text/html"}, False),
    ],
)
def test_only_a_usable_2xx_response_is_worth_reading(
    status: int, headers: dict[str, str], worth: bool
) -> None:
    head = HttpResponse(status, headers, b"")

    assert web_fetch_module._worth_reading(head, 100) is worth


def test_header_names_are_matched_whatever_their_case() -> None:
    upper = {"RETRY-AFTER": "3", "CONTENT-LENGTH": "12"}

    assert web_fetch_module._declared_length(upper) == 12
    assert web_fetch_module._retry_wait(HttpResponse(429, upper, b""), 30.0) == 3.0
