"""Per-host limits for the crawler: concurrency, spacing and Retry-After back-off."""

from __future__ import annotations

import math
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

DEFAULT_HOST_CONCURRENCY = 2
DEFAULT_HOST_DELAY_SECONDS = 0.2
# A server asking for a longer pause than this is left as a retryable failure.
MAX_RETRY_AFTER_SECONDS = 30.0
RETRY_STATUSES = frozenset({429, 503})


@dataclass(frozen=True)
class HostPolicy:
    """How hard one worker pool may lean on a single website host."""

    concurrency: int = DEFAULT_HOST_CONCURRENCY
    delay_seconds: float = DEFAULT_HOST_DELAY_SECONDS
    max_retry_after_seconds: float = MAX_RETRY_AFTER_SECONDS

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("host concurrency must be at least 1")
        if not all(map(math.isfinite, (self.delay_seconds, self.max_retry_after_seconds))):
            raise ValueError("host delays must be finite")
        if self.delay_seconds < 0 or self.max_retry_after_seconds < 0:
            raise ValueError("host delays must not be negative")


def host_of(url: str) -> str:
    """The lowercase host a URL is fetched from."""
    return (urllib.parse.urlsplit(url).hostname or "").lower()


class HostLimiter:
    """Thread-safe gate: at most N requests per host, spaced by a minimum delay."""

    def __init__(
        self,
        policy: HostPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._slots: dict[str, threading.Semaphore] = {}
        self._next_start: dict[str, float] = {}
        self._last_start: dict[str, float] = {}
        self._host_delays: dict[str, float] = {}

    def set_host_delay(self, host: str, seconds: float) -> None:
        """Apply a host's published crawl delay, without weakening local limits."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("host delay must be finite and non-negative")
        with self._lock:
            self._host_delays[host] = seconds
            last_start = self._last_start.get(host)
            if last_start is not None:
                delay = max(self.policy.delay_seconds, seconds)
                self._next_start[host] = max(
                    self._next_start.get(host, last_start + delay), last_start + delay
                )

    @contextmanager
    def slot(self, host: str) -> Iterator[None]:
        """Hold one of the host's request slots, after its spacing has elapsed."""
        with self._lock:
            slots = self._slots.setdefault(host, threading.Semaphore(self.policy.concurrency))
        with slots:
            self._wait_for_start(host)
            yield

    def _wait_for_start(self, host: str) -> None:
        """Wait until the current host deadline, rechecking after every sleep."""
        while True:
            with self._lock:
                now = self._clock()
                start = max(now, self._next_start.get(host, now))
                wait = start - now
                if wait == 0.0:
                    delay = max(self.policy.delay_seconds, self._host_delays.get(host, 0.0))
                    self._last_start[host] = now
                    self._next_start[host] = now + delay
                    return
            self._sleep(wait)

    def back_off(self, host: str, seconds: float) -> None:
        """Hold every request to the host for ``seconds`` from now."""
        with self._lock:
            until = self._clock() + seconds
            self._next_start[host] = max(self._next_start.get(host, until), until)


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` value (delta-seconds or HTTP-date), or ``None``."""
    if value is None:
        return None
    text = value.strip()
    if text.isdecimal():
        return float(text)
    moment = _http_date(text)
    if moment is None:
        return None
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())


def _http_date(text: str) -> datetime | None:
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else None


__all__ = [
    "DEFAULT_HOST_CONCURRENCY",
    "DEFAULT_HOST_DELAY_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "RETRY_STATUSES",
    "HostLimiter",
    "HostPolicy",
    "host_of",
    "retry_after_seconds",
]
