"""Fixtures for the web layer's tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.loopback import LoopbackServer, serve

from osm_polygon_website_tag.web import web_fetch


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopbackServer]:
    """A live server on 127.0.0.1 that the fetch layer treats as a public host.

    The loopback allowance lives in this fixture only: ``_is_public_address`` is
    widened to loopback addresses, so the production check stays untouched.
    """
    original = web_fetch._is_public_address
    monkeypatch.setattr(
        web_fetch,
        "_is_public_address",
        lambda address: address.is_loopback or original(address),
    )
    with serve() as server:
        yield server
