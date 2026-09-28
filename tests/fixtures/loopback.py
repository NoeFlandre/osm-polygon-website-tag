"""A real loopback HTTP server for the web layer's tests."""

from __future__ import annotations

import http.server
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

Body = bytes | Callable[[], Iterator[bytes]]
Route = tuple[int, dict[str, str], Body]


@dataclass
class LoopbackServer:
    """Serve canned routes on 127.0.0.1 and record who connected."""

    routes: dict[str, Route] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    peers: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    port: int = 0
    host: str = "127.0.0.1"

    def url(self, path: str = "/") -> str:
        return f"http://{self.host}:{self.port}{path}"

    def route(self, path: str, body: Body, status: int = 200, **headers: str) -> None:
        self.routes[path] = (status, {k.replace("_", "-"): v for k, v in headers.items()}, body)


def _handler_for(server: LoopbackServer) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            server.requests.append(self.path)
            server.peers.append(self.connection.getsockname()[0])
            server.agents.append(self.headers.get("User-Agent", ""))
            status, headers, body = server.routes.get(self.path, (404, {}, b"missing"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            if isinstance(body, bytes):
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for chunk in [body] if isinstance(body, bytes) else body():
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError:
                return  # the client hung up, which is what the deadline tests want

        def log_message(self, format: str, *args: Any) -> None:
            return None

    return Handler


def drip(count: int, delay: float, byte: bytes = b"x") -> Callable[[], Iterator[bytes]]:
    """A body that trickles ``count`` bytes, one every ``delay`` seconds."""

    def body() -> Iterator[bytes]:
        for _ in range(count):
            time.sleep(delay)
            yield byte

    return body


__all__ = ["LoopbackServer", "drip", "serve"]


@contextmanager
def serve() -> Iterator[LoopbackServer]:
    """Run a server on 127.0.0.1 for the duration of the block."""
    server = LoopbackServer()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(server))
    httpd.daemon_threads = True
    server.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
