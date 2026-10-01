"""Real loopback HTTP setup for the web pipeline acceptance tests."""

from __future__ import annotations

import pytest
from tests.fixtures.loopback_http import LoopbackHTTPFixture, loopback_http_fixture

__all__ = ["loopback_http_fixture"]


@pytest.fixture
def acceptance_http(loopback_http: LoopbackHTTPFixture) -> LoopbackHTTPFixture:
    """Allow the crawler through robots.txt while keeping all sockets local."""
    loopback_http.route(
        "/robots.txt",
        b"User-agent: *\nAllow: /\n",
        headers={"Content-Type": "text/plain"},
    )
    return loopback_http
