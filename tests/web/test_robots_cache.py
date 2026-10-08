"""Behaviour of the injectable robots cache, without network access."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest
from tests.fixtures.memory_http import MemoryHTTPFixture

from osm_polygon_website_tag.web import web_fetch
from osm_polygon_website_tag.web.web_fetch import RobotsCache, fetch_html


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _policy(*, error: bool = False) -> web_fetch._RobotsPolicy:
    if error:
        return web_fetch._RobotsPolicy(
            None, error_status="fetch_error", message="robots_unavailable"
        )
    return web_fetch._RobotsPolicy(web_fetch._AllowAllRobots())


def _recording_loader(
    loads: list[str], *, error: bool = False
) -> Callable[[str], web_fetch._RobotsPolicy]:
    def load(origin: str) -> web_fetch._RobotsPolicy:
        loads.append(origin)
        return _policy(error=error)

    return load


def test_policy_is_loaded_once_and_reused_for_the_origin() -> None:
    cache = RobotsCache()
    loads: list[str] = []
    load = _recording_loader(loads)

    first = cache.get_or_load("https://a.example", load)
    second = cache.get_or_load("https://a.example", load)

    assert second is first
    assert loads == ["https://a.example"]


def test_separate_cache_objects_do_not_share_policies() -> None:
    loads: list[str] = []
    load = _recording_loader(loads)

    RobotsCache().get_or_load("https://a.example", load)
    RobotsCache().get_or_load("https://a.example", load)

    assert loads == ["https://a.example", "https://a.example"]


def test_unbounded_cache_keeps_every_origin() -> None:
    cache = RobotsCache()
    loads: list[str] = []
    load = _recording_loader(loads)
    origins = [f"https://host-{index}.example" for index in range(50)]

    for origin in origins:
        cache.get_or_load(origin, load)
    for origin in origins:
        cache.get_or_load(origin, load)

    assert loads == origins


def test_full_cache_evicts_the_least_recently_used_origin() -> None:
    cache = RobotsCache(max_entries=2)
    loads: list[str] = []
    load = _recording_loader(loads)
    first, second, third = "https://a.example", "https://b.example", "https://c.example"
    cache.get_or_load(first, load)
    cache.get_or_load(second, load)
    cache.get_or_load(first, load)
    loads.clear()

    cache.get_or_load(third, load)
    cache.get_or_load(first, load)
    cache.get_or_load(third, load)
    assert loads == [third]

    loads.clear()
    cache.get_or_load(second, load)
    assert loads == [second]


def test_failed_policy_expires_after_the_error_ttl() -> None:
    clock = FakeClock()
    cache = RobotsCache(error_ttl_seconds=60.0, clock=clock)
    loads: list[str] = []
    load = _recording_loader(loads, error=True)
    origin = "https://flaky.example"

    cache.get_or_load(origin, load)
    clock.now = 59.0
    cache.get_or_load(origin, load)
    assert loads == [origin]

    clock.now = 60.0
    cache.get_or_load(origin, load)
    assert loads == [origin, origin]


def test_successful_policy_is_kept_past_the_error_ttl() -> None:
    clock = FakeClock()
    cache = RobotsCache(error_ttl_seconds=60.0, clock=clock)
    loads: list[str] = []
    load = _recording_loader(loads)
    origin = "https://stable.example"

    cache.get_or_load(origin, load)
    clock.now = 10_000.0
    cache.get_or_load(origin, load)

    assert loads == [origin]


def test_failed_load_is_not_cached_and_the_next_call_loads_again() -> None:
    cache = RobotsCache()
    origin = "https://broken.example"

    def fail(_origin: str) -> web_fetch._RobotsPolicy:
        raise RuntimeError("load failed")

    with pytest.raises(RuntimeError, match="load failed"):
        cache.get_or_load(origin, fail)

    loads: list[str] = []
    cache.get_or_load(origin, _recording_loader(loads))
    assert loads == [origin]


@pytest.mark.parametrize("max_entries", [0, -1])
def test_max_entries_must_allow_at_least_one_origin(max_entries: int) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        RobotsCache(max_entries=max_entries)


def test_fetch_html_uses_the_injected_cache_for_robots_rules(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route(
        "/robots.txt",
        b"User-agent: *\nDisallow: /private\n",
        Content_Type="text/plain",
    )
    memory_http.route("/public/a", b"a", Content_Type="text/html")
    memory_http.route("/public/b", b"b", Content_Type="text/html")
    injected = RobotsCache()

    first = fetch_html(memory_http.url("/public/a"), robots_cache=injected)
    second = fetch_html(memory_http.url("/public/b"), robots_cache=injected)

    assert (first.status, second.status) == ("ok", "ok")
    assert memory_http.requests == ["/robots.txt", "/public/a", "/public/b"]

    # The module default is a different, empty cache, so it loads robots.txt again.
    fetch_html(memory_http.url("/public/a"))
    assert memory_http.requests == [
        "/robots.txt",
        "/public/a",
        "/public/b",
        "/robots.txt",
        "/public/a",
    ]


def test_every_redirect_hop_checks_robots_through_the_injected_cache(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route("/robots.txt", b"User-agent: *\nAllow: /\n", Content_Type="text/plain")
    memory_http.route("/start", b"", status=302, Location=memory_http.url("/middle"))
    memory_http.route("/middle", b"", status=302, Location=memory_http.url("/final"))
    memory_http.route("/final", b"done", Content_Type="text/html")

    result = fetch_html(memory_http.url("/start"), robots_cache=RobotsCache())

    assert result.status == "ok"
    # Each hop reuses the injected policy; the module default would fetch robots.txt again.
    assert memory_http.requests == ["/robots.txt", "/start", "/middle", "/final"]


def test_the_last_redirect_hop_checks_robots_through_the_injected_cache(
    memory_http: MemoryHTTPFixture,
) -> None:
    memory_http.route("/robots.txt", b"User-agent: *\nAllow: /\n", Content_Type="text/plain")
    memory_http.route("/start", b"", status=302, Location=memory_http.url("/final"))
    memory_http.route("/final", b"done", Content_Type="text/html")

    result = fetch_html(memory_http.url("/start"), robots_cache=RobotsCache(), max_redirects=1)

    assert result.status == "ok"
    assert memory_http.requests == ["/robots.txt", "/start", "/final"]


def test_each_polite_fetcher_keeps_its_own_robots_cache(memory_http: MemoryHTTPFixture) -> None:
    memory_http.route("/robots.txt", b"User-agent: *\nAllow: /\n", Content_Type="text/plain")
    memory_http.route("/public/a", b"a", Content_Type="text/html")
    policy = web_fetch.HostPolicy(concurrency=1, delay_seconds=0.0)
    first = web_fetch.make_polite_fetcher(policy)
    second = web_fetch.make_polite_fetcher(policy)

    first(memory_http.url("/public/a"))
    second(memory_http.url("/public/a"))

    assert memory_http.requests.count("/robots.txt") == 2


def test_a_cache_of_one_origin_keeps_that_origin() -> None:
    loads: list[str] = []
    cache = RobotsCache(max_entries=1)
    load = _recording_loader(loads)
    origin = "https://only.example"

    cache.get_or_load(origin, load)
    cache.get_or_load(origin, load)

    assert loads == [origin]


def test_a_full_cache_keeps_exactly_max_entries_origins() -> None:
    loads: list[str] = []
    cache = RobotsCache(max_entries=2)
    load = _recording_loader(loads)
    first, second = "https://a.example", "https://b.example"

    cache.get_or_load(first, load)
    cache.get_or_load(second, load)
    cache.get_or_load(first, load)
    cache.get_or_load(second, load)

    assert loads == [first, second]


def test_concurrent_callers_for_one_origin_load_it_once() -> None:
    cache = RobotsCache()
    origin = "https://busy.example"
    loads: list[str] = []
    results: list[web_fetch._RobotsPolicy] = []
    waiter: threading.Thread | None = None

    def load(loaded_origin: str) -> web_fetch._RobotsPolicy:
        nonlocal waiter
        loads.append(loaded_origin)
        if waiter is None:
            # A second caller arrives while this load is in flight. It must wait for the
            # load and reuse its result, not load the origin again.
            waiter = threading.Thread(
                target=lambda: results.append(cache.get_or_load(origin, load))
            )
            waiter.start()
            time.sleep(0.05)
        return _policy()

    first = cache.get_or_load(origin, load)
    assert waiter is not None
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert loads == [origin]
    assert results[0] is first


def test_the_public_export_list_names_robots_cache() -> None:
    assert "RobotsCache" in web_fetch.__all__
    assert all(hasattr(web_fetch, name) for name in web_fetch.__all__)


@pytest.mark.parametrize("error_ttl_seconds", [float("nan"), float("inf"), -1.0])
def test_error_ttl_must_be_a_finite_non_negative_duration(error_ttl_seconds: float) -> None:
    with pytest.raises(ValueError, match="error_ttl_seconds"):
        RobotsCache(error_ttl_seconds=error_ttl_seconds)


def test_a_zero_error_ttl_reloads_a_failed_policy_on_the_next_call() -> None:
    cache = RobotsCache(error_ttl_seconds=0.0, clock=FakeClock())
    loads: list[str] = []
    load = _recording_loader(loads, error=True)
    origin = "https://flaky.example"

    cache.get_or_load(origin, load)
    cache.get_or_load(origin, load)

    assert loads == [origin, origin]
