"""Canned HTTP responses for tests without sockets or outbound requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from osm_polygon_website_tag.web.web_fetch import HttpResponse

Route = tuple[int, dict[str, str], bytes | BaseException]


@dataclass
class MemoryHTTPFixture:
    """Serve deterministic responses through the fetcher's injected transport."""

    routes: dict[str, Route] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    host: str = "93.184.216.34"

    def url(self, path: str = "/") -> str:
        """Return an absolute URL whose numeric host needs no DNS lookup."""
        return f"http://{self.host}{path}"

    def route(
        self, path: str, body: bytes | BaseException, status: int = 200, **headers: str
    ) -> None:
        """Register one response, normalizing underscores in header names."""
        self.routes[path] = (
            status,
            {name.replace("_", "-"): value for name, value in headers.items()},
            body,
        )

    def download(self, url: str, _timeout: float, _max_bytes: int) -> HttpResponse:
        """Return one registered response or a deterministic 404."""
        path = urlsplit(url).path or "/"
        self.requests.append(path)
        status, headers, body = self.routes.get(path, (404, {}, b"missing"))
        if isinstance(body, BaseException):
            raise body
        response_headers = dict(headers)
        response_headers.setdefault("Content-Length", str(len(body)))
        return HttpResponse(status, response_headers, body)


__all__ = ["MemoryHTTPFixture"]
