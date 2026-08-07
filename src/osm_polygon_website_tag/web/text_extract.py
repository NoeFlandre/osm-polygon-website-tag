"""Small deterministic Trafilatura adapter for downloaded HTML."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from typing import Literal

import trafilatura
from trafilatura.settings import Extractor

from osm_polygon_website_tag.contracts.text_schema import count_words


@dataclass(frozen=True)
class TextExtraction:
    """Structured main-text extraction result."""

    status: Literal["success", "empty", "extract_error"]
    text: str | None
    word_count: int | None
    message: str | None
    trafilatura_version: str


_extractor_state = threading.local()


@lru_cache(maxsize=1)
def _trafilatura_version() -> str:
    """Resolve the installed Trafilatura version once per process."""
    return version("trafilatura")


def _extractor_options(url: str) -> Extractor:
    """Reuse per-thread Trafilatura setup while updating the current URL."""
    options = getattr(_extractor_state, "options", None)
    if options is None:
        options = Extractor(output_format="txt", comments=False, tables=True)
        _extractor_state.options = options
    options.url = url
    options.source = url.encode("utf-8", "replace").decode("utf-8")
    return options


def extract_main_text(html: bytes, *, url: str) -> TextExtraction:
    """Extract full main text from already downloaded HTML."""
    library_version = _trafilatura_version()
    decoded = html.decode("utf-8", errors="replace")
    try:
        value = trafilatura.extract(
            decoded,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            options=_extractor_options(url),
        )
    except Exception as exc:
        return TextExtraction(
            "extract_error",
            None,
            None,
            type(exc).__name__,
            library_version,
        )
    if value is None or not value.strip():
        return TextExtraction("empty", "", 0, None, library_version)
    return TextExtraction("success", value, count_words(value), None, library_version)


__all__ = ["TextExtraction", "extract_main_text"]
