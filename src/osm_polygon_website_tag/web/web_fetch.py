"""Bounded HTTP downloader for untrusted OSM website values."""

from __future__ import annotations

import http.client
import ipaddress
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from osm_polygon_website_tag import __version__
from osm_polygon_website_tag.web.content_type import charset_parameter, media_type
from osm_polygon_website_tag.web.politeness import (
    RETRY_STATUSES,
    HostLimiter,
    HostPolicy,
    host_of,
    retry_after_seconds,
)

MAX_RESPONSE_BYTES = 20_000_000
REQUEST_TIMEOUT_SECONDS = 30.0
ROBOTS_TIMEOUT_SECONDS = 5.0
ROBOTS_MAX_BYTES = 500_000
MAX_REDIRECTS = 3
READ_CHUNK_BYTES = 65_536
USER_AGENT = f"osm-polygon-website-tag/{__version__} (+https://github.com/NoeFlandre/osm-polygon-website-tag)"

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

    status: Literal["ok", "invalid_url", "unsafe_url", "fetch_error", "robots_disallowed"]
    requested_url: str
    final_url: str | None = None
    body: bytes | None = None
    message: str | None = None
    charset: str | None = None
    media_type: str | None = None


@dataclass(frozen=True)
class _RobotsPolicy:
    """Cached robots rules and any safe-transport failure for one origin."""

    parser: urllib.robotparser.RobotFileParser | _AllowAllRobots | None
    error_status: Literal["unsafe_url", "fetch_error"] | None = None
    final_url: str | None = None
    message: str | None = None
    crawl_delay: float | None = None


class _AllowAllRobots:
    """Robots rules that allow every agent and path: a missing or malformed robots.txt."""

    def can_fetch(self, _useragent: str, _url: str) -> bool:
        return True

    def crawl_delay(self, _useragent: str) -> None:
        return None


@dataclass(frozen=True)
class _CachedPolicy:
    """A robots policy and the clock time it was stored."""

    policy: _RobotsPolicy
    stored_at: float


class RobotsCache:
    """Per-origin robots policies, shared by the fetches that receive this object.

    With no bounds, a policy stays for the life of the object. ``max_entries``
    evicts the least recently used origin when the cache is full.
    ``error_ttl_seconds`` expires the policy of a failed robots fetch (unsafe,
    unavailable or unreadable), so that origin is requested again later.
    Concurrent callers for one origin wait for a single load.
    """

    def __init__(
        self,
        *,
        max_entries: int | None = None,
        error_ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries is not None and max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._error_ttl_seconds = error_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _CachedPolicy] = OrderedDict()
        self._load_locks: dict[str, threading.Lock] = {}

    def get_or_load(self, origin: str, load: Callable[[str], _RobotsPolicy]) -> _RobotsPolicy:
        """Return the cached policy for ``origin``, loading it at most once at a time."""
        cached = self._lookup(origin)
        if cached is not None:
            return cached
        load_lock = self._load_lock(origin)
        try:
            with load_lock:
                cached = self._lookup(origin)
                if cached is not None:
                    return cached
                policy = load(origin)
                self._store(origin, policy)
                return policy
        finally:
            self._release_load_lock(origin, load_lock)

    def _lookup(self, origin: str) -> _RobotsPolicy | None:
        with self._lock:
            entry = self._entries.get(origin)
            if entry is None:
                return None
            if self._is_expired(entry):
                del self._entries[origin]
                return None
            self._entries.move_to_end(origin)
            return entry.policy

    def _is_expired(self, entry: _CachedPolicy) -> bool:
        if self._error_ttl_seconds is None or entry.policy.error_status is None:
            return False
        return self._clock() - entry.stored_at >= self._error_ttl_seconds

    def _store(self, origin: str, policy: _RobotsPolicy) -> None:
        with self._lock:
            self._entries[origin] = _CachedPolicy(policy, self._clock())
            self._entries.move_to_end(origin)
            while self._max_entries is not None and len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def _load_lock(self, origin: str) -> threading.Lock:
        with self._lock:
            return self._load_locks.setdefault(origin, threading.Lock())

    def _release_load_lock(self, origin: str, load_lock: threading.Lock) -> None:
        # The lock is only needed while a load is in flight. Later callers find
        # the stored policy first, so the per-origin lock does not accumulate.
        with self._lock:
            if self._load_locks.get(origin) is load_lock:
                del self._load_locks[origin]


_DEFAULT_ROBOTS_CACHE = RobotsCache()
# Bounds for the per-fetcher caches (#135). The module default stays unbounded.
ROBOTS_CACHE_MAX_ORIGINS = 4096
ROBOTS_ERROR_TTL_SECONDS = 3600.0


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
    if ":" in value.partition("/")[0]:
        raise ValueError("unsupported_scheme")
    return "https://" + value


def _normalise_http_hostname(parsed: urllib.parse.SplitResult) -> str:
    """Validate and IDNA-normalize the hostname from a parsed URL."""
    _reject_url_credentials(parsed)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("missing_hostname")
    hostname = hostname.rstrip(_TRAILING_DOT)  # SplitResult.hostname is already lowercase
    if not hostname:
        raise ValueError("missing_hostname")
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
        return hostname.encode(_IDNA).decode(_ASCII)
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
    limiter: HostLimiter | None = None,
    robots_cache: RobotsCache | None = None,
) -> FetchResult:
    """Fetch one HTML document while validating each redirect target.

    With a ``limiter``, requests to one host are capped and spaced, and a 429 or
    503 that carries a short ``Retry-After`` is retried once after the pause.
    Robots policies come from ``robots_cache``; by default they come from the
    module's shared cache.
    """
    requested_or_error = _normalise_requested_url(raw_url)
    if isinstance(requested_or_error, FetchResult):
        return requested_or_error
    requested = requested_or_error
    transport = request_once or _download_once
    return _follow_redirects(
        requested,
        transport if limiter is None else _polite(transport, limiter),
        resolver,
        timeout_seconds,
        max_bytes,
        max_redirects,
        limiter=limiter,
        robots_cache=robots_cache,
    )


def make_bounded_robots_cache() -> RobotsCache:
    """A robots cache for one fetcher: at most 4096 origins; failed fetches retry after an hour."""
    return RobotsCache(
        max_entries=ROBOTS_CACHE_MAX_ORIGINS, error_ttl_seconds=ROBOTS_ERROR_TTL_SECONDS
    )


def make_polite_fetcher(policy: HostPolicy) -> Callable[[str], FetchResult]:
    """A ``fetch_html`` that shares one per-host limiter and one bounded robots cache."""
    limiter = HostLimiter(policy)
    cache = make_bounded_robots_cache()
    return lambda url: fetch_html(url, limiter=limiter, robots_cache=cache)


def _polite(transport: RequestOnce, limiter: HostLimiter) -> RequestOnce:
    """Wrap a transport with the host limits and one bounded Retry-After retry."""

    def request(url: str, timeout_seconds: float, max_bytes: int) -> HttpResponse:
        host = host_of(url)
        response, wait = _throttled(
            transport,
            limiter,
            host,
            (url, timeout_seconds, max_bytes),
            retry_after_cap=limiter.policy.max_retry_after_seconds,
        )
        if wait is None:
            return response
        return _throttled(
            transport,
            limiter,
            host,
            (url, timeout_seconds, max_bytes),
            retry_after_cap=limiter.policy.max_retry_after_seconds,
        )[0]

    return request


def _throttled(
    transport: RequestOnce,
    limiter: HostLimiter,
    host: str,
    args: tuple[str, float, int],
    *,
    retry_after_cap: float | None = None,
) -> tuple[HttpResponse, float | None]:
    with limiter.slot(host):
        response = transport(*args)
        wait = _retry_wait(response, retry_after_cap) if retry_after_cap is not None else None
        if wait is not None:
            limiter.back_off(host, wait)
        return response, wait


def _retry_wait(response: HttpResponse, cap: float) -> float | None:
    """Seconds to pause before retrying, when the server asked for a short one."""
    if response.status_code not in RETRY_STATUSES:
        return None
    wait = retry_after_seconds(_header(response.headers, _RETRY_AFTER))
    return wait if wait is not None and wait <= cap else None


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
    *,
    limiter: HostLimiter | None = None,
    check_robots: bool = True,
    robots_cache: RobotsCache | None = None,
) -> FetchResult:
    """Fetch a normalized URL while validating each redirect target."""
    if max_redirects < 0:
        raise AssertionError("redirect loop exhausted")
    current = requested
    for _ in range(max_redirects):
        outcome = _request_redirect_hop(
            current,
            requested,
            transport,
            resolver,
            timeout_seconds,
            max_bytes,
            limiter,
            check_robots=check_robots,
            robots_cache=robots_cache,
        )
        if isinstance(outcome, FetchResult):
            return outcome
        current = outcome

    outcome = _request_redirect_hop(
        current,
        requested,
        transport,
        resolver,
        timeout_seconds,
        max_bytes,
        limiter,
        check_robots=check_robots,
        robots_cache=robots_cache,
    )
    if isinstance(outcome, FetchResult):
        return outcome
    return FetchResult("fetch_error", requested, final_url=current, message="redirect_limit")


def _request_redirect_hop(
    current: str,
    requested: str,
    transport: RequestOnce,
    resolver: Resolver,
    timeout_seconds: float,
    max_bytes: int,
    limiter: HostLimiter | None,
    *,
    check_robots: bool,
    robots_cache: RobotsCache | None,
) -> FetchResult | str:
    """Check policy and execute one bounded redirect hop."""
    if check_robots:
        robots_result = _check_robots(
            current, requested, transport, resolver, limiter, robots_cache
        )
        if robots_result is not None:
            return robots_result
    next_url, result = _fetch_step(
        current,
        requested,
        transport,
        resolver,
        timeout_seconds,
        max_bytes,
    )
    return result if result is not None else _require_redirect_url(next_url)


def _check_robots(
    current: str,
    requested: str,
    transport: RequestOnce,
    resolver: Resolver,
    limiter: HostLimiter | None,
    robots_cache: RobotsCache | None,
) -> FetchResult | None:
    """Return a robots policy failure or allow this redirect target."""
    cache = _DEFAULT_ROBOTS_CACHE if robots_cache is None else robots_cache
    policy = _robots_policy(current, transport, resolver, cache)
    return _apply_robots_policy(policy, current, requested, limiter)


def _apply_robots_policy(
    policy: _RobotsPolicy,
    current: str,
    requested: str,
    limiter: HostLimiter | None,
) -> FetchResult | None:
    """Apply policy errors and crawl delay before testing the requested path."""
    failure = _robots_policy_error(policy, current, requested)
    if failure is not None:
        return failure
    _honor_robots_crawl_delay(policy, current, limiter)
    return _robots_disallowed_result(policy, current, requested)


def _robots_policy_error(policy: _RobotsPolicy, current: str, requested: str) -> FetchResult | None:
    if policy.error_status is None:
        return None
    return FetchResult(
        policy.error_status,
        requested,
        final_url=policy.final_url or current,
        message=policy.message,
    )


def _honor_robots_crawl_delay(
    policy: _RobotsPolicy,
    current: str,
    limiter: HostLimiter | None,
) -> None:
    if limiter is None or policy.crawl_delay is None:
        return
    limiter.set_host_delay(host_of(current), policy.crawl_delay)


def _robots_disallowed_result(
    policy: _RobotsPolicy, current: str, requested: str
) -> FetchResult | None:
    if policy.parser is None or policy.parser.can_fetch(USER_AGENT, current):
        return None
    return FetchResult(
        "robots_disallowed",
        requested,
        final_url=current,
        message="robots_disallowed",
    )


def _require_redirect_url(next_url: str | None) -> str:
    """Enforce the redirect step's one-terminal-value contract."""
    if next_url is None:  # pragma: no cover - _fetch_step always returns a result
        raise AssertionError("fetch step returned neither a result nor a redirect")
    return next_url


def _robots_policy(
    url: str,
    transport: RequestOnce,
    resolver: Resolver,
    cache: RobotsCache,
) -> _RobotsPolicy:
    """Return one cached, safely fetched robots policy for a URL's origin."""
    parsed = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return cache.get_or_load(
        origin,
        lambda loaded_origin: _fetch_robots_policy(loaded_origin, transport, resolver),
    )


def _fetch_robots_policy(
    origin: str,
    transport: RequestOnce,
    resolver: Resolver,
) -> _RobotsPolicy:
    """Fetch and parse one origin's policy without reapplying robots checks."""
    robots_url = urllib.parse.urljoin(f"{origin}/", "robots.txt")
    fetched = _follow_redirects(
        robots_url,
        transport,
        resolver,
        ROBOTS_TIMEOUT_SECONDS,
        ROBOTS_MAX_BYTES,
        MAX_REDIRECTS,
        check_robots=False,
    )
    failure = _robots_fetch_failure(fetched)
    if failure is not None:
        return failure
    assert fetched.body is not None
    return _parse_robots_policy(robots_url, fetched.body)


def _robots_fetch_failure(fetched: FetchResult) -> _RobotsPolicy | None:
    """Classify unsafe, absent, or unavailable robots responses."""
    if fetched.status == "unsafe_url":
        return _RobotsPolicy(
            None,
            error_status="unsafe_url",
            final_url=fetched.final_url,
            message="robots_unsafe_url",
        )
    if _robots_response_is_missing(fetched):
        return _RobotsPolicy(_AllowAllRobots())
    return None if _robots_response_is_ok(fetched) else _robots_unavailable_policy(fetched)


def _robots_response_is_missing(fetched: FetchResult) -> bool:
    return fetched.status == "fetch_error" and fetched.message in {"http_404", "http_410"}


def _robots_response_is_ok(fetched: FetchResult) -> bool:
    return fetched.status == "ok" and fetched.body is not None


def _robots_unavailable_policy(fetched: FetchResult) -> _RobotsPolicy:
    return _RobotsPolicy(
        None,
        error_status="fetch_error",
        final_url=fetched.final_url,
        message="robots_unavailable",
    )


def _parse_robots_policy(robots_url: str, body: bytes) -> _RobotsPolicy:
    """Parse one bounded robots response, allowing malformed policy on error."""
    parser = urllib.robotparser.RobotFileParser(robots_url)
    try:
        parser.parse(body.decode(errors="replace").splitlines())
    except Exception:  # noqa: BLE001 - malformed robots text is treated as no rules
        return _RobotsPolicy(_AllowAllRobots())
    crawl_delay, valid = _robots_crawl_delay(parser)
    if not valid:
        return _RobotsPolicy(_AllowAllRobots())
    return _RobotsPolicy(parser, crawl_delay=crawl_delay)


def _robots_crawl_delay(
    parser: urllib.robotparser.RobotFileParser,
) -> tuple[float | None, bool]:
    """Return a finite nonnegative delay and whether parsing it succeeded."""
    try:
        value = parser.crawl_delay(USER_AGENT)
        if value is None:
            return None, True
        delay = float(value)
    except Exception:  # noqa: BLE001 - malformed crawl delay is treated as no rules
        return None, False
    if not math.isfinite(delay) or delay < 0:
        return None, True
    return delay, True


def _fetch_step(
    current: str,
    requested: str,
    transport: RequestOnce,
    resolver: Resolver,
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[str | None, FetchResult | None]:
    """Validate, request, and classify one redirect-loop iteration."""
    response_or_error = _safe_request(
        current, requested, transport, resolver, timeout_seconds, max_bytes
    )
    if isinstance(response_or_error, FetchResult):
        return None, response_or_error
    response = response_or_error
    if 300 <= response.status_code < 400:
        return _redirect_step(response, current, requested)
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
    except UnsafeUrlError:
        return FetchResult("unsafe_url", requested, final_url=current, message="unsafe_url")
    except Exception as exc:  # noqa: BLE001 - any transport failure becomes a fetch_error result
        return FetchResult("fetch_error", requested, final_url=current, message=type(exc).__name__)


def _redirect_step(
    response: HttpResponse,
    current: str,
    requested: str,
) -> tuple[str | None, FetchResult | None]:
    """Resolve one redirect response or return its terminal error."""
    location = _header(response.headers, _LOCATION)
    if location is None:
        return None, FetchResult(
            "fetch_error", requested, final_url=current, message="redirect_without_location"
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
    message = _response_error(response, max_bytes)
    if message is not None:
        return FetchResult("fetch_error", requested, final_url=current, message=message)
    return FetchResult(
        "ok",
        requested,
        final_url=current,
        body=response.body,
        charset=_header_charset(response.headers),
        media_type=_header_media_type(response.headers),
    )


def _response_error(response: HttpResponse, max_bytes: int) -> str | None:
    """Name why a non-redirect response is unusable, checking status, size, then type.

    Everything but the body length is decided from the headers alone, so the
    transport can refuse a page before downloading it.
    """
    if not 200 <= response.status_code < 300:
        return f"http_{response.status_code}"
    if _too_large(response, max_bytes):
        return "response_too_large"
    if not _media_type_allowed(response.headers):
        return "unsupported_content_type"
    return None


def _too_large(response: HttpResponse, max_bytes: int) -> bool:
    """Whether the body, or the length the server announced, exceeds the limit."""
    declared = _declared_length(response.headers)
    return len(response.body) > max_bytes or (declared is not None and declared > max_bytes)


def _declared_length(headers: Mapping[str, str]) -> int | None:
    """Return the Content-Length header as an integer, ignoring malformed values."""
    value = _header(headers, _CONTENT_LENGTH)
    return int(value) if value is not None and value.strip().isdecimal() else None


def _media_type_allowed(headers: Mapping[str, str]) -> bool:
    """Require a declared Content-Type that is an accepted document type."""
    declared = _header_media_type(headers)
    return declared in _DOCUMENT_TYPES


_CONTENT_TYPE = "content-type"
# Names and codec labels that mutation testing cannot tell from their aliases:
# header lookups are case-insensitive and codec labels have case-insensitive aliases.
_LOCATION = "location"
_IDNA = "idna"
_ASCII = "ascii"
_TRAILING_DOT = "."
# Header names are literals here, not inline, because ``_header`` matches them
# case-insensitively and urllib normalises the case of request headers: a
# differently-cased spelling inline is a mutant no test could tell apart.
_CONTENT_LENGTH = "content-length"
_RETRY_AFTER = "retry-after"
_USER_AGENT_HEADER = "User-Agent"
_DOCUMENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})


def _header_charset(headers: Mapping[str, str]) -> str | None:
    """Return the ``charset=`` parameter of the Content-Type header, if any."""
    return charset_parameter(str(_header(headers, _CONTENT_TYPE)))


def _header_media_type(headers: Mapping[str, str]) -> str | None:
    value = _header(headers, _CONTENT_TYPE)
    return None if value is None else media_type(value)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,  # noqa: ARG002 - urllib's override signature
        fp: Any,  # noqa: ARG002
        code: int,  # noqa: ARG002
        msg: str,  # noqa: ARG002
        headers: Any,  # noqa: ARG002
        newurl: str,  # noqa: ARG002
    ) -> None:
        return None


def _connect_public(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
    """Open a TCP connection and reject it unless the connected peer is public.

    ``validate_public_http_url`` resolves the host before the request, but the
    socket layer resolves it again; a DNS answer that changes in between
    (rebinding) would otherwise reach a private address. Checking the peer
    before any HTTP or TLS byte is sent closes that window.
    """
    sock = socket.create_connection(address, *args, **kwargs)
    peer = ipaddress.ip_address(sock.getpeername()[0])
    if not _is_public_address(peer):
        sock.close()
        raise UnsafeUrlError("non_global_address")
    return sock


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_PublicHTTPSConnection, req)


def _read_before_deadline(
    response: Any, limit: int, deadline: float, clock: Callable[[], float] = time.monotonic
) -> bytes:
    """Read at most ``limit`` bytes, failing once the whole-request deadline passes.

    The socket timeout bounds each blocking read, not the request; a server
    trickling one byte per timeout window would otherwise hold a worker for hours.
    ``read1`` returns after one underlying read; ``read`` would block until the
    whole chunk arrived, so the deadline would only be checked once a dripping
    server had finished.
    """
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        if clock() >= deadline:
            raise TimeoutError("request deadline exceeded")
        chunk = response.read1(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _download_once(url: str, timeout_seconds: float, max_bytes: int) -> HttpResponse:
    deadline = time.monotonic() + timeout_seconds
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        _PublicHTTPHandler(),
        _PublicHTTPSHandler(),
    )
    # The caller has normalized and validated HTTP(S) immediately before this call.
    request = urllib.request.Request(url, headers={_USER_AGENT_HEADER: USER_AGENT})  # noqa: S310
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        response = exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, UnsafeUrlError):
            raise exc.reason from exc
        raise
    with response:
        head = _response_head(response)
        # A redirect, an error page, a video or an oversized file is refused
        # from its headers; downloading it first wastes up to max_bytes each.
        if not _worth_reading(head, max_bytes):
            return head
        body = _read_before_deadline(response, max_bytes + 1, deadline)
        return HttpResponse(head.status_code, dict(head.headers), body)


def _response_head(response: Any) -> HttpResponse:
    """The status and headers of a urllib response, with an empty body."""
    status = response.status
    return HttpResponse(0 if status is None else int(status), dict(response.headers.items()), b"")


def _worth_reading(head: HttpResponse, max_bytes: int) -> bool:
    """Only a 2xx response whose headers pass every check has a body we keep."""
    return _response_error(head, max_bytes) is None


__all__ = [
    "FetchResult",
    "HttpResponse",
    "UnsafeUrlError",
    "fetch_html",
    "make_polite_fetcher",
    "normalize_http_url",
    "validate_public_http_url",
]
