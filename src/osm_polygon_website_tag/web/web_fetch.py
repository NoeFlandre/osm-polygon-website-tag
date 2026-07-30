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
    value = raw.strip()
    if not value:
        raise ValueError("empty_url")
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        if ":" in value.split("/", 1)[0]:
            raise ValueError("unsupported_scheme")
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials_not_allowed")
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("missing_hostname")
    hostname = hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("localhost_not_allowed")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid_hostname") from exc
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
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not _is_public_address(literal):
        raise UnsafeUrlError("non_global_address")
    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise UnsafeUrlError("dns_resolution_failed") from exc
    if not addresses:
        raise UnsafeUrlError("dns_resolution_empty")
    for address in addresses:
        sockaddr = address[4]
        ip = ipaddress.ip_address(sockaddr[0])
        if not _is_public_address(ip):
            raise UnsafeUrlError("non_global_address")
    return True


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
    try:
        requested = normalize_http_url(raw_url)
    except (ValueError, UnicodeError):
        return FetchResult("invalid_url", raw_url, message="invalid_url")
    current = requested
    transport = request_once or _download_once
    for redirect_number in range(max_redirects + 1):
        try:
            validate_public_http_url(current, resolver=resolver)
        except UnsafeUrlError:
            return FetchResult("unsafe_url", requested, final_url=current, message="unsafe_url")
        try:
            response = transport(current, timeout_seconds, max_bytes)
        except Exception as exc:
            return FetchResult(
                "fetch_error",
                requested,
                final_url=current,
                message=type(exc).__name__,
            )
        if 300 <= response.status_code < 400:
            location = _header(response.headers, "location")
            if location is None:
                return FetchResult(
                    "fetch_error",
                    requested,
                    final_url=current,
                    message="redirect_without_location",
                )
            if redirect_number == max_redirects:
                return FetchResult(
                    "fetch_error",
                    requested,
                    final_url=current,
                    message="redirect_limit",
                )
            try:
                current = normalize_http_url(urllib.parse.urljoin(current, location))
            except ValueError:
                return FetchResult(
                    "invalid_url",
                    requested,
                    final_url=current,
                    message="invalid_redirect",
                )
            continue
        if not 200 <= response.status_code < 300:
            return FetchResult(
                "fetch_error",
                requested,
                final_url=current,
                message=f"http_{response.status_code}",
            )
        if len(response.body) > max_bytes:
            return FetchResult(
                "fetch_error",
                requested,
                final_url=current,
                message="response_too_large",
            )
        content_type = (_header(response.headers, "content-type") or "").lower()
        if content_type and not any(
            allowed in content_type
            for allowed in ("text/html", "application/xhtml+xml", "text/plain")
        ):
            return FetchResult(
                "fetch_error",
                requested,
                final_url=current,
                message="unsupported_content_type",
            )
        return FetchResult("ok", requested, final_url=current, body=response.body)
    raise AssertionError("redirect loop exhausted")  # pragma: no cover


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
