"""Robots rules are fetched through the safe, injected transport."""

from __future__ import annotations

import threading
import urllib.robotparser
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from tests.fixtures.memory_http import MemoryHTTPFixture

from osm_polygon_website_tag.web import web_fetch
from osm_polygon_website_tag.web.politeness import HostLimiter, HostPolicy
from osm_polygon_website_tag.web.web_fetch import FetchResult, fetch_html


def test_allow_all_robots_parser_allows_every_user_agent_and_path() -> None:
    robots_url = "https://example.org/robots.txt"

    parser = web_fetch._allow_all_robots_parser(robots_url)

    assert cast(Any, parser).url == robots_url
    assert cast(Any, parser).allow_all is True
    assert parser.can_fetch("osm-polygon-website-tag", "https://example.org/private")
    assert parser.can_fetch("another-agent", "https://example.org/anything")


def test_apply_robots_policy_returns_errors_without_changing_host_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = HostLimiter(HostPolicy(concurrency=1, delay_seconds=0))
    delays: list[tuple[str, float]] = []
    monkeypatch.setattr(
        limiter, "set_host_delay", lambda host, seconds: delays.append((host, seconds))
    )
    current = "https://example.org/page"
    requested = "https://example.org/start"
    policy = web_fetch._RobotsPolicy(
        None,
        error_status="fetch_error",
        final_url="https://example.org/robots.txt",
        message="robots_unavailable",
        crawl_delay=5.0,
    )

    result = web_fetch._apply_robots_policy(policy, current, requested, limiter)

    assert result == FetchResult(
        "fetch_error",
        requested,
        final_url="https://example.org/robots.txt",
        message="robots_unavailable",
    )
    assert delays == []


def test_disallowed_robots_policy_still_sets_its_host_delay() -> None:
    parser = urllib.robotparser.RobotFileParser("https://example.org/robots.txt")
    parser.parse(["User-agent: *", "Disallow: /private"])
    policy = web_fetch._RobotsPolicy(parser, crawl_delay=3.0)
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
    requested = "https://example.org/start"
    current = "https://example.org/private"

    result = web_fetch._apply_robots_policy(policy, current, requested, limiter)
    with limiter.slot("example.org"):
        pass
    with limiter.slot("example.org"):
        pass

    assert result == FetchResult(
        "robots_disallowed",
        requested,
        final_url=current,
        message="robots_disallowed",
    )
    assert waits == [3.0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (None, True)),
        (0, (0.0, True)),
        (2, (2.0, True)),
        (-1, (None, True)),
        (float("inf"), (None, True)),
        (float("nan"), (None, True)),
        ("invalid", (None, False)),
    ],
)
def test_robots_crawl_delay_accepts_only_finite_nonnegative_values(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
    expected: tuple[float | None, bool],
) -> None:
    parser = urllib.robotparser.RobotFileParser()
    agents: list[str] = []
    monkeypatch.setattr(
        parser,
        "crawl_delay",
        lambda agent: agents.append(agent) or value,
    )

    result = web_fetch._robots_crawl_delay(parser)

    assert result == expected
    assert agents == [web_fetch.USER_AGENT]


def test_robots_crawl_delay_treats_parser_exceptions_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = urllib.robotparser.RobotFileParser()

    def fail(_agent: str) -> None:
        raise ValueError("bad crawl delay")

    monkeypatch.setattr(parser, "crawl_delay", fail)

    assert web_fetch._robots_crawl_delay(parser) == (None, False)


def test_parse_robots_policy_preserves_rules_and_valid_crawl_delay() -> None:
    robots_url = "https://example.org/robots.txt"
    policy = web_fetch._parse_robots_policy(
        robots_url,
        b"User-agent: *\nDisallow: /private\nCrawl-delay: 2\n",
    )

    assert policy.crawl_delay == 2.0
    assert policy.parser is not None
    assert policy.parser.can_fetch("osm-polygon-website-tag", "https://example.org/public")
    assert not policy.parser.can_fetch("osm-polygon-website-tag", "https://example.org/private")


@pytest.mark.parametrize(
    ("fetched", "expected_status", "expected_message"),
    [
        (
            FetchResult(
                "unsafe_url",
                "https://example.org/robots.txt",
                final_url="http://127.0.0.1/robots.txt",
            ),
            "unsafe_url",
            "robots_unsafe_url",
        ),
        (
            FetchResult("fetch_error", "https://example.org/robots.txt", message="http_404"),
            None,
            None,
        ),
        (
            FetchResult("fetch_error", "https://example.org/robots.txt", message="http_410"),
            None,
            None,
        ),
        (
            FetchResult("fetch_error", "https://example.org/robots.txt", message="TimeoutError"),
            "fetch_error",
            "robots_unavailable",
        ),
        (
            FetchResult("ok", "https://example.org/robots.txt", body=None),
            "fetch_error",
            "robots_unavailable",
        ),
    ],
)
def test_robots_fetch_failure_classifies_unsafe_missing_and_unavailable_responses(
    fetched: FetchResult,
    expected_status: Literal["unsafe_url", "fetch_error"] | None,
    expected_message: str | None,
) -> None:
    robots_url = "https://example.org/robots.txt"

    policy = web_fetch._robots_fetch_failure(robots_url, fetched)

    if expected_status is None:
        assert policy is not None and policy.parser is not None
        assert policy.parser.can_fetch(web_fetch.USER_AGENT, "https://example.org/private")
    else:
        assert policy == web_fetch._RobotsPolicy(
            None,
            error_status=expected_status,
            final_url=fetched.final_url,
            message=expected_message,
        )


def test_fetch_robots_policy_uses_bounded_fetch_without_recursing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = "https://example.org"
    calls: list[tuple[object, ...]] = []

    def follow(
        url: str,
        transport: web_fetch.RequestOnce,
        resolver: web_fetch.Resolver,
        timeout: float,
        max_bytes: int,
        max_redirects: int,
        *,
        check_robots: bool,
    ) -> FetchResult:
        calls.append((url, transport, resolver, timeout, max_bytes, max_redirects, check_robots))
        return FetchResult(
            "ok",
            url,
            final_url=url,
            body=b"User-agent: *\nDisallow: /private\n",
        )

    monkeypatch.setattr(web_fetch, "_follow_redirects", follow)

    def transport(_url: str, _timeout: float, _max_bytes: int) -> web_fetch.HttpResponse:
        return web_fetch.HttpResponse(200, {}, b"")

    def resolver(_host: str, _port: int) -> list[tuple[Any, ...]]:
        return []

    policy = web_fetch._fetch_robots_policy(origin, transport, resolver)

    assert calls == [
        (
            f"{origin}/robots.txt",
            transport,
            resolver,
            web_fetch.ROBOTS_TIMEOUT_SECONDS,
            web_fetch.ROBOTS_MAX_BYTES,
            web_fetch.MAX_REDIRECTS,
            False,
        )
    ]
    assert policy.parser is not None
    assert not policy.parser.can_fetch(web_fetch.USER_AGENT, f"{origin}/private")


def test_robots_policy_caches_by_origin_but_separates_scheme_and_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = f"cache-{id(tmp_path)}.example"
    loaded: list[str] = []

    def load(origin: str, _transport: object, _resolver: object) -> web_fetch._RobotsPolicy:
        loaded.append(origin)
        return web_fetch._RobotsPolicy(web_fetch._allow_all_robots_parser(f"{origin}/robots.txt"))

    monkeypatch.setattr(web_fetch, "_fetch_robots_policy", load)

    def transport(_url: str, _timeout: float, _max_bytes: int) -> web_fetch.HttpResponse:
        return web_fetch.HttpResponse(200, {}, b"")

    def resolver(_host: str, _port: int) -> list[tuple[Any, ...]]:
        return []

    first = web_fetch._robots_policy(f"https://{host}/one", transport, resolver)
    same_origin = web_fetch._robots_policy(f"https://{host}/two?next=1", transport, resolver)
    other_scheme = web_fetch._robots_policy(f"http://{host}/one", transport, resolver)
    other_port = web_fetch._robots_policy(f"https://{host}:8443/one", transport, resolver)

    assert same_origin is first
    assert other_scheme is not first
    assert other_port is not first
    assert loaded == [f"https://{host}", f"http://{host}", f"https://{host}:8443"]


def test_robots_policy_loads_an_origin_once_for_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = f"concurrent-{id(tmp_path)}.example"
    entered = threading.Event()
    second_load = threading.Event()
    release = threading.Event()
    attempts: list[str] = []
    attempts_lock = threading.Lock()

    def load(origin: str, _transport: object, _resolver: object) -> web_fetch._RobotsPolicy:
        with attempts_lock:
            attempts.append(origin)
            if len(attempts) == 1:
                entered.set()
            else:
                second_load.set()
        release.wait(timeout=2)
        return web_fetch._RobotsPolicy(web_fetch._allow_all_robots_parser(f"{origin}/robots.txt"))

    monkeypatch.setattr(web_fetch, "_fetch_robots_policy", load)

    def transport(_url: str, _timeout: float, _max_bytes: int) -> web_fetch.HttpResponse:
        return web_fetch.HttpResponse(200, {}, b"")

    def resolver(_host: str, _port: int) -> list[tuple[Any, ...]]:
        return []

    results: list[web_fetch._RobotsPolicy] = []

    def fetch() -> None:
        results.append(web_fetch._robots_policy(f"https://{host}/page", transport, resolver))

    first = threading.Thread(target=fetch)
    second = threading.Thread(target=fetch)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    try:
        assert not second_load.wait(timeout=0.1)
    finally:
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert attempts == [f"https://{host}"]
    assert len(results) == 2 and results[0] is results[1]


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


def test_user_agent_specific_robots_rule_blocks_redirect_target(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route(
        "/robots.txt",
        b"User-agent: osm-polygon-website-tag\nDisallow: /private\nUser-agent: *\nAllow: /\n",
        Content_Type="text/plain",
    )
    requested_url = memory_http.url("/start")
    final_url = memory_http.url("/private")
    memory_http.route("/start", b"", status=302, Location=final_url)
    memory_http.route("/private", b"private", Content_Type="text/html")

    result = fetch_html(requested_url)

    assert result == FetchResult(
        "robots_disallowed",
        requested_url,
        final_url=final_url,
        message="robots_disallowed",
    )
    assert memory_http.requests == ["/robots.txt", "/start"]


def test_robots_error_preserves_requested_and_policy_urls() -> None:
    requested_url = "https://start.example/page"
    current_url = "https://redirect.example/private"
    robots_url = "https://redirect.example/robots.txt"
    policy = web_fetch._RobotsPolicy(
        None,
        error_status="fetch_error",
        final_url=robots_url,
        message="robots_unavailable",
    )

    result = web_fetch._robots_policy_error(policy, current_url, requested_url)

    assert result == FetchResult(
        "fetch_error",
        requested_url,
        final_url=robots_url,
        message="robots_unavailable",
    )


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
    assert result.requested_url == memory_http.url("/page")
    assert result.final_url == memory_http.url("/robots.txt")
    assert memory_http.requests == ["/robots.txt"]


def test_gone_robots_file_allows_the_page(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/robots.txt", b"", status=410, Content_Type="text/plain")
    memory_http.route("/page", b"page", Content_Type="text/html")

    result = fetch_html(memory_http.url("/page"))

    assert result.status == "ok"
    assert result.body == b"page"
    assert memory_http.requests == ["/robots.txt", "/page"]


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


def test_invalid_utf8_does_not_hide_valid_disallow_rule(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route(
        "/robots.txt",
        b"\xff\nUser-agent: *\nDisallow: /private\n",
        Content_Type="text/plain",
    )
    memory_http.route("/private", b"private", Content_Type="text/html")

    result = fetch_html(memory_http.url("/private"))

    assert result.status == "robots_disallowed"
    assert result.final_url == memory_http.url("/private")
    assert memory_http.requests == ["/robots.txt"]


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
