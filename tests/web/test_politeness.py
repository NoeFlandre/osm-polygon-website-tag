"""Per-host limits: concurrency, spacing, back-off and Retry-After parsing."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from osm_polygon_website_tag.web.politeness import (
    HostLimiter,
    HostPolicy,
    host_of,
    retry_after_seconds,
)


class _Clock:
    """A fake clock whose sleep advances it, so waits cost no real time."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if not 0 < seconds <= 30.0:
            pytest.fail(f"unexpected host wait: {seconds}")
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(clock: _Clock, *, concurrency: int, delay_seconds: float) -> HostLimiter:
    return HostLimiter(HostPolicy(concurrency, delay_seconds), clock=clock, sleep=clock.sleep)


def test_defaults_are_conservative() -> None:
    policy = HostPolicy()

    assert (policy.concurrency, policy.delay_seconds, policy.max_retry_after_seconds) == (
        2,
        0.2,
        30.0,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"concurrency": 0},
        {"concurrency": -1},
        {"delay_seconds": -0.1},
        {"delay_seconds": float("nan")},
        {"delay_seconds": float("inf")},
        {"max_retry_after_seconds": -1},
        {"max_retry_after_seconds": float("nan")},
        {"max_retry_after_seconds": float("inf")},
    ],
)
def test_invalid_policies_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="host"):
        HostPolicy(**kwargs)


def test_zero_delay_and_zero_retry_cap_are_valid() -> None:
    assert HostPolicy(concurrency=1, delay_seconds=0, max_retry_after_seconds=0).concurrency == 1


@pytest.mark.parametrize(
    ("url", "host"),
    [("https://Example.ORG/a?b=1", "example.org"), ("http://a.b:8080/", "a.b"), ("mailto:x", "")],
)
def test_host_of_lowercases_the_hostname(url: str, host: str) -> None:
    assert host_of(url) == host


def test_requests_to_one_host_are_spaced_by_the_delay() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=2.0)
    starts = []

    for _ in range(3):
        with limiter.slot("a.example"):
            starts.append(clock.now)

    assert starts == [100.0, 102.0, 104.0]
    assert clock.sleeps == [2.0, 2.0]


def test_first_request_never_sleeps_before_a_host_deadline_exists() -> None:
    def unexpected_sleep(seconds: float) -> None:
        pytest.fail(f"first request slept for {seconds} seconds")

    limiter = HostLimiter(
        HostPolicy(concurrency=1, delay_seconds=0),
        clock=lambda: 100.0,
        sleep=unexpected_sleep,
    )

    with limiter.slot("a.example"):
        pass


def test_different_hosts_do_not_wait_for_each_other() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=5.0)

    for host in ("a.example", "b.example", "c.example"):
        with limiter.slot(host):
            pass

    assert clock.sleeps == []


def test_a_request_after_the_delay_has_passed_does_not_wait() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=2.0)
    with limiter.slot("a.example"):
        pass
    clock.now += 10

    with limiter.slot("a.example"):
        pass

    assert clock.sleeps == []


def test_unconfigured_host_does_not_receive_a_default_delay() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=0.0)
    starts: list[float] = []

    for _ in range(2):
        with limiter.slot("a.example"):
            starts.append(clock.now)

    assert starts == [100.0, 100.0]
    assert clock.sleeps == []


@pytest.mark.parametrize(("host_delay", "expected_wait"), [(5.0, 5.0), (1.0, 2.0)])
def test_host_delay_after_a_request_keeps_the_stronger_spacing(
    host_delay: float,
    expected_wait: float,
) -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=2.0)
    with limiter.slot("a.example"):
        pass

    limiter.set_host_delay("a.example", host_delay)
    with limiter.slot("a.example"):
        pass

    assert clock.sleeps == [expected_wait]


@pytest.mark.parametrize("host_delay", [0.0, 0.5])
def test_small_nonnegative_host_delays_are_applied(host_delay: float) -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=0.0)
    limiter.set_host_delay("a.example", host_delay)
    starts: list[float] = []

    for _ in range(2):
        with limiter.slot("a.example"):
            starts.append(clock.now)

    assert starts == [100.0, 100.0 + host_delay]
    assert clock.sleeps == ([host_delay] if host_delay else [])


def test_host_delay_configured_before_first_request_controls_later_spacing() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=0.0)
    limiter.set_host_delay("a.example", 3.0)
    starts: list[float] = []

    for _ in range(2):
        with limiter.slot("a.example"):
            starts.append(clock.now)

    assert starts == [100.0, 103.0]
    assert clock.sleeps == [3.0]


@pytest.mark.parametrize("seconds", [-1.0, float("nan"), float("inf")])
def test_invalid_host_delay_is_rejected(seconds: float) -> None:
    limiter = HostLimiter(HostPolicy())

    with pytest.raises(ValueError, match=r"^host delay must be finite and non-negative$"):
        limiter.set_host_delay("a.example", seconds)


def test_back_off_holds_the_host_and_never_shortens_a_longer_hold() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=0.0)

    limiter.back_off("a.example", 7.0)
    limiter.back_off("a.example", 3.0)
    with limiter.slot("a.example"):
        pass
    with limiter.slot("b.example"):
        pass

    assert clock.sleeps == [7.0]


def test_host_delay_never_shortens_an_existing_retry_backoff() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=0.0)
    starts: list[float] = []

    with limiter.slot("a.example"):
        starts.append(clock.now)
    limiter.back_off("a.example", 30.0)
    limiter.set_host_delay("a.example", 5.0)
    with limiter.slot("a.example"):
        starts.append(clock.now)

    assert starts == [100.0, 130.0]
    assert clock.sleeps == [30.0]


def test_back_off_on_an_unseen_host_sets_the_hold() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=1, delay_seconds=0.0)

    limiter.back_off("a.example", 4.0)
    with limiter.slot("a.example"):
        pass

    assert clock.sleeps == [4.0]


def test_back_off_extends_a_wait_already_in_progress() -> None:
    clock = _Clock()
    limiter = _limiter(clock, concurrency=2, delay_seconds=10.0)
    starts: list[float] = []

    with limiter.slot("a.example"):
        starts.append(clock.now)

    real_sleep = clock.sleep
    extend_once = True

    def sleep_with_back_off(seconds: float) -> None:
        nonlocal extend_once
        if extend_once:
            extend_once = False
            limiter.back_off("a.example", 20.0)
        real_sleep(seconds)

    limiter._sleep = sleep_with_back_off
    with limiter.slot("a.example"):
        starts.append(clock.now)

    assert starts == [100.0, 120.0]
    assert clock.sleeps == [10.0, 10.0]


def test_a_host_never_has_more_requests_in_flight_than_its_limit() -> None:
    limiter = HostLimiter(HostPolicy(concurrency=2, delay_seconds=0.0))
    lock = threading.Lock()
    active = {"now": 0, "peak": 0}
    release = threading.Event()

    def worker() -> None:
        with limiter.slot("a.example"):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            release.wait(timeout=5)
            with lock:
                active["now"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    deadline = threading.Event()
    while active["now"] < 2 and not deadline.wait(0.01):
        pass
    deadline.wait(0.1)
    assert active["peak"] == 2
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert active["peak"] == 2


def test_another_host_is_not_blocked_by_a_full_host() -> None:
    limiter = HostLimiter(HostPolicy(concurrency=1, delay_seconds=0.0))
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with limiter.slot("a.example"):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(timeout=5)
    with limiter.slot("b.example"):
        other_ran = True
    release.set()
    thread.join(timeout=5)

    assert other_ran


_NOW = datetime(2026, 10, 21, 7, 27, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2", 2.0),
        (" 120 ", 120.0),
        ("0", 0.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 60.0),
        ("Wed, 21 Oct 2026 07:26:00 GMT", 0.0),
        ("-5", None),
        ("1.5", None),
        ("soon", None),
        ("", None),
        (None, None),
        ("Wed, 21 Oct 2026 07:28:00 -0000", None),
    ],
)
def test_retry_after_parses_seconds_and_http_dates(
    value: str | None, expected: float | None
) -> None:
    assert retry_after_seconds(value, now=_NOW) == expected


def test_retry_after_dates_are_measured_from_the_given_now() -> None:
    wait = retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT", now=_NOW)

    assert wait == (datetime(2099, 10, 21, 7, 28, tzinfo=UTC) - _NOW).total_seconds()


def test_retry_after_dates_default_to_the_current_utc_time() -> None:
    target = datetime(2099, 10, 21, 7, 28, tzinfo=UTC)
    before = datetime.now(UTC)
    wait = retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT")
    after = datetime.now(UTC)

    assert wait is not None
    assert (target - after).total_seconds() <= wait <= (target - before).total_seconds()


def test_a_missing_retry_after_is_none_not_an_empty_date() -> None:
    assert retry_after_seconds(None) is None
    assert retry_after_seconds("") is None
