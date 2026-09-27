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


_COMMENT = re.compile(rb"<!--.*?(?:-->|$)", re.DOTALL)
_META_TAG = re.compile(rb"<meta\s[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(rb"""([A-Za-z-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_CONTENT_CHARSET = re.compile(rb"charset\s*=\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)
# Byte-order marks win over any declaration, as in the WHATWG sniffing algorithm.
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _tag_charset(tag: bytes) -> bytes | None:
    """Charset of one ``<meta charset>`` or ``http-equiv="Content-Type"`` tag."""
    attributes = {name.lower(): value.strip(b"\"'") for name, value in _ATTRIBUTE.findall(tag)}
    if b"charset" in attributes:
        return attributes[b"charset"]
    if attributes.get(b"http-equiv", b"").lower() != b"content-type":
        return None
    match = _CONTENT_CHARSET.search(attributes.get(b"content", b""))
    return match.group(1) if match else None


def _meta_charset(html: bytes) -> str | None:
    """Return the codec declared by the first active meta charset tag, if valid."""
    head = _COMMENT.sub(b"", html[:4096])
    for tag in _META_TAG.findall(head):
        declared = _tag_charset(tag)
        if declared is not None:
            return _codec(declared.decode("ascii", "replace"))
    return None


def _codec(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _bom_codec(html: bytes) -> str | None:
    return next((codec for bom, codec in _BOMS if html.startswith(bom)), None)


def _wide_codec(charset: str | None) -> str | None:
    """Declared UTF-16/32 codec: ASCII text in those is also valid UTF-8."""
    codec = _codec(charset)
    return codec if codec is not None and codec.startswith(("utf-16", "utf-32")) else None


def decode_html(html: bytes, charset: str | None = None) -> str:
    """Decode HTML: BOM, declared UTF-16/32, valid UTF-8, charset, meta, detection."""
    candidates = [_bom_codec(html), _wide_codec(charset), "utf-8"]
    candidates += [_codec(charset), _meta_charset(html), *detect_encoding(html)]
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
