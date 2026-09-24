"""Hermetic tests for bounded and SSRF-safe website downloads."""

from __future__ import annotations

import socket

import pytest

import osm_polygon_website_tag.web.web_fetch as web_fetch_module
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


def test_download_once_reads_response_without_following_redirects(monkeypatch) -> None:
    class Response:
        status = 200

        def __init__(self) -> None:
            self.headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == 11
            return b"hello"

    class Opener:
        def open(self, request, *, timeout: float):
            assert request.full_url == "https://example.org"
            assert timeout == 3.0
            return Response()

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_args: Opener())

    result = web_fetch_module._download_once("https://example.org", 3.0, 10)

    assert result == HttpResponse(200, {"Content-Type": "text/plain"}, b"hello")


def test_private_fetch_classifiers_and_redirect_helpers_are_deterministic() -> None:
    response = HttpResponse(200, {"Content-Type": "text/html"}, b"body")
    assert web_fetch_module._header(response.headers, "content-type") == "text/html"
    assert web_fetch_module._header(response.headers, "missing") is None
    assert web_fetch_module._status_error(response, "https://example.org", "requested", 10) is None
    assert web_fetch_module._size_error(response, "https://example.org", "requested", 3)
    assert web_fetch_module._content_type_error(
        HttpResponse(200, {"Content-Type": "image/png"}, b"x"),
        "https://example.org",
        "requested",
        10,
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


def test_connect_returns_public_peer_socket(monkeypatch) -> None:
    sock = _FakeSocket("93.184.216.34")
    calls = []

    def create_connection(*args, **kwargs):
        calls.append((args, kwargs))
        return sock

    monkeypatch.setattr(web_fetch_module.socket, "create_connection", create_connection)

    assert web_fetch_module._connect_public(("example.org", 443), 3.0, None) is sock
    assert calls == [((("example.org", 443), 3.0, None), {})]
    assert not sock.closed


@pytest.mark.parametrize(
    ("connection", "handler", "method"),
    [
        ("_PublicHTTPConnection", "_PublicHTTPHandler", "http_open"),
        ("_PublicHTTPSConnection", "_PublicHTTPSHandler", "https_open"),
    ],
)
def test_handlers_open_peer_checked_connections(
    monkeypatch, connection: str, handler: str, method: str
) -> None:
    conn = getattr(web_fetch_module, connection)("example.org")
    assert conn._create_connection is web_fetch_module._connect_public
    seen = []
    monkeypatch.setattr(
        getattr(web_fetch_module, handler),
        "do_open",
        lambda _self, cls, req: seen.append((cls, req)) or "response",
    )

    assert getattr(getattr(web_fetch_module, handler)(), method)("req") == "response"
    assert seen == [(getattr(web_fetch_module, connection), "req")]


def test_download_once_surfaces_connect_time_unsafe_error(monkeypatch) -> None:
    class Opener:
        def open(self, _request, *, timeout: float):
            raise web_fetch_module.urllib.error.URLError(UnsafeUrlError("non_global_address"))

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_args: Opener())

    with pytest.raises(UnsafeUrlError):
        web_fetch_module._download_once("https://example.org", 3.0, 10)


def test_download_once_reraises_other_url_errors(monkeypatch) -> None:
    class Opener:
        def open(self, _request, *, timeout: float):
            raise web_fetch_module.urllib.error.URLError("refused")

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_args: Opener())

    with pytest.raises(web_fetch_module.urllib.error.URLError):
        web_fetch_module._download_once("https://example.org", 3.0, 10)


# --- Behaviour pins: every branch below is observable and mutation-gated. ---

_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _public_resolver(_host: str, _port: int):
    return _PUBLIC


def test_module_constants_are_pinned() -> None:
    assert web_fetch_module.MAX_RESPONSE_BYTES == 20_000_000
    assert web_fetch_module.REQUEST_TIMEOUT_SECONDS == 30.0
    assert web_fetch_module.MAX_REDIRECTS == 3
    assert web_fetch_module.USER_AGENT == (
        "osm-polygon-website-tag/0.1 (+https://github.com/NoeFlandre/osm-polygon-website-tag)"
    )


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


def test_fetch_default_resolver_and_limits(monkeypatch) -> None:
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


def test_fetch_uses_download_once_by_default(monkeypatch) -> None:
    calls = []

    def fake(url: str, timeout: float, max_bytes: int) -> HttpResponse:
        calls.append((url, timeout, max_bytes))
        return HttpResponse(200, {}, b"ok")

    monkeypatch.setattr(web_fetch_module, "_download_once", fake)
    result = fetch_html("https://93.184.216.34/", timeout_seconds=2.5, max_bytes=5)
    assert result == FetchResult("ok", "https://93.184.216.34/", "https://93.184.216.34/", b"ok")
    assert calls == [("https://93.184.216.34/", 2.5, 5)]


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
    assert result == FetchResult("ok", "https://example.org", final_url=final, body=b"done")
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


@pytest.mark.parametrize("status", [300, 301, 399])
def test_redirect_statuses_follow_location(status: int) -> None:
    seen = []

    def request(url: str, *_a) -> HttpResponse:
        seen.append(url)
        if len(seen) == 1:
            return HttpResponse(status, {"location": "/n"}, b"")
        return HttpResponse(200, {}, b"b")

    result = fetch_html("https://example.org", request_once=request, resolver=_public_resolver)
    assert result == FetchResult("ok", "https://example.org", "https://example.org/n", b"b")


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
    assert web_fetch_module._status_error(
        HttpResponse(status, {}, b""), "cur", "req", 0
    ) == FetchResult("fetch_error", "req", final_url="cur", message=f"http_{status}")


@pytest.mark.parametrize("status", [200, 204, 299])
def test_status_ok_2xx(status: int) -> None:
    assert web_fetch_module._status_error(HttpResponse(status, {}, b""), "c", "r", 0) is None
    assert fetch_html(
        "https://example.org",
        request_once=lambda *_a: HttpResponse(status, {}, b"x"),
        resolver=_public_resolver,
    ) == FetchResult("ok", "https://example.org", "https://example.org", b"x")


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
    size = web_fetch_module._size_error
    assert size(HttpResponse(200, {}, b"abc"), "c", "r", 3) is None
    assert size(HttpResponse(200, {}, b"abcd"), "c", "r", 3) == FetchResult(
        "fetch_error", "r", final_url="c", message="response_too_large"
    )
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
        request_once=lambda *_a: HttpResponse(200, {}, b"abc"),
        resolver=_public_resolver,
        max_bytes=3,
    ) == FetchResult("ok", "https://example.org", "https://example.org", b"abc")


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "TEXT/HTML; charset=utf-8",
        "application/xhtml+xml",
        "Application/XHTML+XML; charset=utf-8",
        "text/plain",
        "text/plain; charset=latin-1",
        "",
    ],
)
def test_content_type_allowed(content_type: str) -> None:
    response = HttpResponse(200, {"Content-Type": content_type}, b"x")
    assert web_fetch_module._content_type_error(response, "c", "r", 0) is None
    assert fetch_html(
        "https://example.org", request_once=lambda *_a: response, resolver=_public_resolver
    ) == FetchResult("ok", "https://example.org", "https://example.org", b"x")


@pytest.mark.parametrize(
    "content_type", ["image/png", "application/json", "text/css", "application/xml", "XX"]
)
def test_content_type_rejected(content_type: str) -> None:
    response = HttpResponse(200, {"CONTENT-TYPE": content_type}, b"x")
    expected = FetchResult("fetch_error", "r", final_url="c", message="unsupported_content_type")
    assert web_fetch_module._content_type_error(response, "c", "r", 0) == expected
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
        HttpResponse(200, {}, b"z"), "cur", "req", 1
    ) == FetchResult("ok", "req", final_url="cur", body=b"z")


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


def test_download_once_builds_safe_opener_and_request(monkeypatch) -> None:
    seen = {}

    class Response:
        status = 201
        headers = {"A": "b"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, limit: int) -> bytes:
            seen["limit"] = limit
            return b"x"

    class Opener:
        def open(self, request, *, timeout: float):
            seen["request"] = request
            seen["timeout"] = timeout
            return Response()

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", build_opener)
    result = web_fetch_module._download_once("http://example.org/p", 4.0, 0)
    assert result == HttpResponse(201, {"A": "b"}, b"x")
    assert seen["limit"] == 1
    assert seen["timeout"] == 4.0
    assert seen["request"].get_header("User-agent") == web_fetch_module.USER_AGENT
    handlers = seen["handlers"]
    assert [type(h) for h in handlers] == [
        web_fetch_module.urllib.request.ProxyHandler,
        web_fetch_module._NoRedirect,
        web_fetch_module._PublicHTTPHandler,
        web_fetch_module._PublicHTTPSHandler,
    ]
    assert handlers[0].proxies == {}


@pytest.mark.parametrize(("raw_status", "expected"), [(None, 0), (404, 404), ("503", 503)])
def test_download_once_http_error_and_missing_status(monkeypatch, raw_status, expected) -> None:
    import email.message
    import io

    headers = email.message.Message()
    headers["Content-Type"] = "text/html"

    class Err(web_fetch_module.urllib.error.HTTPError):
        @property
        def status(self):
            return raw_status

    err = Err("https://example.org", 404, "nf", headers, io.BytesIO(b"abcdef"))

    class Opener:
        def open(self, _request, *, timeout: float):
            raise err

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_a: Opener())
    result = web_fetch_module._download_once("https://example.org", 1.0, 3)
    assert result == HttpResponse(expected, {"Content-Type": "text/html"}, b"abcd")
    assert type(result.status_code) is int


def test_download_once_unsafe_reason_is_chained(monkeypatch) -> None:
    reason = UnsafeUrlError("non_global_address")
    url_error = web_fetch_module.urllib.error.URLError(reason)

    class Opener:
        def open(self, _request, *, timeout: float):
            raise url_error

    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_a: Opener())
    with pytest.raises(UnsafeUrlError) as info:
        web_fetch_module._download_once("https://example.org", 3.0, 10)
    assert info.value is reason
    assert info.value.__cause__ is url_error


def test_connect_public_forwards_kwargs(monkeypatch) -> None:
    sock = _FakeSocket("2606:2800:220:1::1")
    calls = []
    monkeypatch.setattr(
        web_fetch_module.socket,
        "create_connection",
        lambda *a, **k: calls.append((a, k)) or sock,
    )
    assert web_fetch_module._connect_public(("h", 1), timeout=2.0, source_address=None) is sock
    assert calls == [((("h", 1),), {"timeout": 2.0, "source_address": None})]
    assert not sock.closed


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
