"""Shared setup for hermetic web tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from osm_polygon_website_tag.web import web_fetch


@dataclass(frozen=True)
class _Route:
    status: int
    headers: dict[str, str]
    body: bytes
    drip_interval: float | None = None


@dataclass
class LoopbackHTTPFixture:
    """A real HTTP server whose only reachable destination is its own socket."""

    server: ThreadingHTTPServer
    routes: dict[str, _Route]
    requests: list[str] = field(default_factory=list)
    resolved_addresses: list[str] = field(default_factory=list)
    connected_addresses: list[tuple[str, int]] = field(default_factory=list)

    @property
    def port(self) -> int:
        """The sole TCP port permitted by this fixture."""
        return int(self.server.server_address[1])

    def url(self, path: str) -> str:
        """Build a URL for this local server."""
        return f"http://127.0.0.1:{self.port}{path}"

    def route(
        self,
        path: str,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        drip_interval: float | None = None,
    ) -> None:
        """Register a response, optionally sending its body one byte at a time."""
        self.routes[path] = _Route(status, headers or {}, body, drip_interval)

    def resolve(self, host: str, port: int) -> list[tuple[Any, ...]]:
        """Resolve only the configured loopback origin and record the peer IP."""
        if (host, port) != ("127.0.0.1", self.port):
            raise web_fetch.UnsafeUrlError("loopback_fixture_allowlist")
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        self.resolved_addresses.extend(str(answer[4][0]) for answer in answers)
        return answers


def _make_handler(routes: dict[str, _Route], requests: list[str]) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler that serves only the fixture's canned routes."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            requests.append(path)
            route = routes.get(path, _Route(404, {}, b"missing"))
            self.send_response(route.status)
            for name, value in route.headers.items():
                self.send_header(name, value)
            if not any(name.lower() == "content-length" for name in route.headers):
                self.send_header("Content-Length", str(len(route.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if route.drip_interval is None:
                    self.wfile.write(route.body)
                else:
                    for byte in route.body:
                        self.wfile.write(bytes((byte,)))
                        self.wfile.flush()
                        time.sleep(route.drip_interval)
            except BrokenPipeError:
                # A deadline test intentionally closes its socket mid-response.
                return

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _restrict_to_loopback_fixture(
    fixture: LoopbackHTTPFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permit the fixture address while refusing all other client sockets."""
    original_public_check = web_fetch._is_public_address
    original_connect = socket.create_connection

    def allow_test_loopback(address: IPv4Address | IPv6Address) -> bool:
        return bool(address.is_loopback) or original_public_check(address)

    def connect_only_to_fixture(
        address: tuple[str, int], *args: Any, **kwargs: Any
    ) -> socket.socket:
        if address != ("127.0.0.1", fixture.port):
            raise AssertionError(f"loopback fixture blocked outbound connection to {address!r}")
        fixture.connected_addresses.append(address)
        return original_connect(address, *args, **kwargs)

    monkeypatch.setattr(web_fetch, "_is_public_address", allow_test_loopback)
    monkeypatch.setattr(web_fetch.socket, "create_connection", connect_only_to_fixture)


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


@pytest.fixture
def loopback_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopbackHTTPFixture]:
    """Serve local HTTP while rejecting every client socket except this port."""
    routes: dict[str, _Route] = {}
    requests: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(routes, requests))
    server.daemon_threads = True
    fixture = LoopbackHTTPFixture(server, routes, requests)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    _restrict_to_loopback_fixture(fixture, monkeypatch)
    monkeypatch.setattr(web_fetch, "_ROBOTS_CACHE", {})
    monkeypatch.setattr(web_fetch, "_ROBOTS_LOAD_LOCKS", {})
    try:
        yield fixture
    finally:
        server.shutdown()
        server_thread.join(timeout=2)
        server.server_close()


__all__ = ["LoopbackHTTPFixture"]
