"""Bounded HTTP downloader for untrusted OSM website values."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

MAX_RESPONSE_BYTES = 20_000_000
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 3
USER_AGENT = "osm-polygon-website-tag/0.1 (+https://github.com/NoeFlandre/osm-polygon-website-tag)"

Resolver = Callable[[str, int], list[tuple[Any, ...]]]
RequestOnce = Callable[[str, float, int], "HttpResponse"]


class UnsafeUrlError(ValueError):
    """Raised when a URL can resolve to a non-public network target."""


@dataclass(frozen=True)
class HttpResponse:
    """Minimal transport response used by the safe redirect loop."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class FetchResult:
    """Structured website download result."""

    status: Literal["ok", "invalid_url", "unsafe_url", "fetch_error"]
    requested_url: str
    final_url: str | None = None
    body: bytes | None = None
    message: str | None = None


def normalize_http_url(raw: str) -> str:
    """Normalize an absolute, scheme-relative, or bare HTTP website value."""
    value = _coerce_http_value(raw)
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    hostname = _normalise_http_hostname(parsed)
    port = parsed.port
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    host_part = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_part if port in {None, default_port} else f"{host_part}:{port}"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path,
            parsed.query,
            "",
        )
    )


def _coerce_http_value(raw: str) -> str:
    """Add a safe HTTP scheme to a raw website value."""
    value = raw.strip()
    if not value:
        raise ValueError("empty_url")
    if value.startswith("//"):
        return "https:" + value
    if "://" in value:
        return value
    if ":" in value.split("/", 1)[0]:
        raise ValueError("unsupported_scheme")
    return "https://" + value


def _normalise_http_hostname(parsed: urllib.parse.SplitResult) -> str:
    """Validate and IDNA-normalize the hostname from a parsed URL."""
    _reject_url_credentials(parsed)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("missing_hostname")
    hostname = hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("localhost_not_allowed")
    return _encode_hostname(hostname)


def _reject_url_credentials(parsed: urllib.parse.SplitResult) -> None:
    """Reject username/password components before any network use."""
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials_not_allowed")


def _encode_hostname(hostname: str) -> str:
    """Encode a validated hostname using IDNA."""
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid_hostname") from exc


def validate_public_http_url(
    url: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> bool:
    """Require every resolved address for ``url`` to be globally routable."""
    normalized = normalize_http_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    assert parsed.hostname is not None
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _validate_literal_host(host)
    addresses = _resolve_addresses(resolver, host, port)
    _validate_resolved_addresses(addresses)
    return True


def _validate_literal_host(host: str) -> None:
    """Reject a literal IP address unless it is globally routable."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return
    if not _is_public_address(literal):
        raise UnsafeUrlError("non_global_address")


def _resolve_addresses(resolver: Resolver, host: str, port: int) -> list[tuple[Any, ...]]:
    """Resolve a hostname and normalize resolver failures."""
    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise UnsafeUrlError("dns_resolution_failed") from exc
    if not addresses:
        raise UnsafeUrlError("dns_resolution_empty")
    return addresses


def _validate_resolved_addresses(addresses: list[tuple[Any, ...]]) -> None:
    """Require every DNS answer to be globally routable."""
    for address in addresses:
        sockaddr = address[4]
        ip = ipaddress.ip_address(sockaddr[0])
        if not _is_public_address(ip):
            raise UnsafeUrlError("non_global_address")


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def fetch_html(
    raw_url: str,
    *,
    request_once: RequestOnce | None = None,
    resolver: Resolver = socket.getaddrinfo,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResult:
    """Fetch one HTML document while validating each redirect target."""
    requested_or_error = _normalise_requested_url(raw_url)
    if isinstance(requested_or_error, FetchResult):
        return requested_or_error
    requested = requested_or_error
    return _follow_redirects(
        requested,
        request_once or _download_once,
        resolver,
        timeout_seconds,
        max_bytes,
        max_redirects,
    )


def _normalise_requested_url(raw_url: str) -> str | FetchResult:
    """Normalize the initial URL, converting syntax errors to fetch results."""
    try:
        return normalize_http_url(raw_url)
    except (ValueError, UnicodeError):
        return FetchResult("invalid_url", raw_url, message="invalid_url")


def _follow_redirects(
    requested: str,
    transport: RequestOnce,
    resolver: Resolver,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
) -> FetchResult:
    """Fetch a normalized URL while validating each redirect target."""
    current = requested
    for redirect_number in range(max_redirects + 1):
        next_url, result = _fetch_step(
            current,
            requested,
            transport,
            resolver,
            timeout_seconds,
            max_bytes,
            redirect_number,
            max_redirects,
        )
        if result is not None:
            return result
        if next_url is None:  # pragma: no cover - _fetch_step always returns one terminal value
            raise AssertionError("fetch step returned neither a result nor a redirect")
        current = next_url
    raise AssertionError("redirect loop exhausted")  # pragma: no cover


def _fetch_step(
    current: str,
    requested: str,
    transport: RequestOnce,
    resolver: Resolver,
    timeout_seconds: float,
    max_bytes: int,
    redirect_number: int,
    max_redirects: int,
) -> tuple[str | None, FetchResult | None]:
    """Validate, request, and classify one redirect-loop iteration."""
    response_or_error = _safe_request(
        current, requested, transport, resolver, timeout_seconds, max_bytes
    )
    if isinstance(response_or_error, FetchResult):
        return None, response_or_error
    response = response_or_error
    if 300 <= response.status_code < 400:
        return _redirect_step(response, current, requested, redirect_number, max_redirects)
    return None, _terminal_response(response, current, requested, max_bytes)


def _safe_request(
    current: str,
    requested: str,
    transport: RequestOnce,
    resolver: Resolver,
    timeout_seconds: float,
    max_bytes: int,
) -> HttpResponse | FetchResult:
    """Validate a target and perform one transport request."""
    try:
        validate_public_http_url(current, resolver=resolver)
    except UnsafeUrlError:
        return FetchResult("unsafe_url", requested, final_url=current, message="unsafe_url")
    try:
        return transport(current, timeout_seconds, max_bytes)
    except Exception as exc:
        return FetchResult("fetch_error", requested, final_url=current, message=type(exc).__name__)


def _redirect_step(
    response: HttpResponse,
    current: str,
    requested: str,
    redirect_number: int,
    max_redirects: int,
) -> tuple[str | None, FetchResult | None]:
    """Resolve one redirect response or return its terminal error."""
    location = _header(response.headers, "location")
    if location is None:
        return None, FetchResult(
            "fetch_error", requested, final_url=current, message="redirect_without_location"
        )
    if redirect_number == max_redirects:
        return None, FetchResult(
            "fetch_error", requested, final_url=current, message="redirect_limit"
        )
    try:
        return normalize_http_url(urllib.parse.urljoin(current, location)), None
    except ValueError:
        return None, FetchResult(
            "invalid_url", requested, final_url=current, message="invalid_redirect"
        )


def _terminal_response(
    response: HttpResponse,
    current: str,
    requested: str,
    max_bytes: int,
) -> FetchResult:
    """Classify a non-redirect response and enforce body/content limits."""
    for checker in (_status_error, _size_error, _content_type_error):
        error = checker(response, current, requested, max_bytes)
        if error is not None:
            return error
    return FetchResult("ok", requested, final_url=current, body=response.body)


def _status_error(
    response: HttpResponse,
    current: str,
    requested: str,
    _max_bytes: int,
) -> FetchResult | None:
    """Return an error for non-2xx responses."""
    if not 200 <= response.status_code < 300:
        return FetchResult(
            "fetch_error", requested, final_url=current, message=f"http_{response.status_code}"
        )
    return None


def _size_error(
    response: HttpResponse,
    current: str,
    requested: str,
    max_bytes: int,
) -> FetchResult | None:
    """Return an error when the response body exceeds the configured limit."""
    if len(response.body) > max_bytes:
        return FetchResult(
            "fetch_error", requested, final_url=current, message="response_too_large"
        )
    return None


def _content_type_error(
    response: HttpResponse,
    current: str,
    requested: str,
    _max_bytes: int,
) -> FetchResult | None:
    """Return an error for non-document content types."""
    content_type = (_header(response.headers, "content-type") or "").lower()
    if content_type and not any(
        allowed in content_type for allowed in ("text/html", "application/xhtml+xml", "text/plain")
    ):
        return FetchResult(
            "fetch_error", requested, final_url=current, message="unsupported_content_type"
        )
    return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _download_once(url: str, timeout_seconds: float, max_bytes: int) -> HttpResponse:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    # The caller has normalized and validated HTTP(S) immediately before this call.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(max_bytes + 1)
        status = response.status
        if status is None:
            status = 0
        return HttpResponse(int(status), dict(response.headers.items()), body)


__all__ = [
    "FetchResult",
    "HttpResponse",
    "UnsafeUrlError",
    "fetch_html",
    "normalize_http_url",
    "validate_public_http_url",
]
