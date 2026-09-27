"""Small deterministic Trafilatura adapter for downloaded HTML."""

from __future__ import annotations

import codecs
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from typing import Literal

import trafilatura
from trafilatura.settings import Extractor
from trafilatura.utils import detect_encoding

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


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)


def _meta_charset(html: bytes) -> str | None:
    """Return the codec named by a ``<meta charset>`` in the page head, if valid."""
    match = _META_CHARSET.search(html[:4096])
    if match is None:
        return None
    return _codec(match.group(1).decode("ascii"))


def _codec(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def decode_html(html: bytes, charset: str | None = None) -> str:
    """Decode HTML bytes: valid UTF-8, then HTTP charset, ``<meta charset>``, detection."""
    try:
        return html.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates = [_codec(charset), _meta_charset(html), *detect_encoding(html)]
    for encoding in candidates:
        if encoding is None:
            continue
        try:
            return html.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return html.decode("utf-8", errors="replace")


def extract_main_text(html: bytes, *, url: str, charset: str | None = None) -> TextExtraction:
    """Extract full main text from already downloaded HTML."""
    library_version = _trafilatura_version()
    decoded = decode_html(html, charset)
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


__all__ = ["TextExtraction", "decode_html", "extract_main_text"]
