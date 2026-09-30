"""Shared setup for hermetic web tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fixtures.loopback_http import LoopbackHTTPFixture, loopback_http_fixture

from osm_polygon_website_tag.web import web_fetch


@pytest.fixture(autouse=True)
def _skip_robots_for_transport_unit_tests(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Keep transport unit tests focused; policy integration tests opt in."""
    if Path(str(request.node.path)).name == "test_robots_policy.py":
        return

    def allow_all(
        _url: str, _transport: web_fetch.RequestOnce, _resolver: web_fetch.Resolver
    ) -> web_fetch._RobotsPolicy:
        return web_fetch._RobotsPolicy(None)

    monkeypatch.setattr(web_fetch, "_robots_policy", allow_all)


__all__ = ["LoopbackHTTPFixture", "loopback_http_fixture"]
