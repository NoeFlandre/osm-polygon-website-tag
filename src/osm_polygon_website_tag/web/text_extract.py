"""Small deterministic Trafilatura adapter for downloaded HTML."""

from __future__ import annotations

import codecs
import re
import threading
from collections.abc import Iterable
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

# Module-level literals: codec names are case-insensitive, so spelling them
# inside functions only adds mutants no test can tell apart.
_UTF8 = "utf-8"
_LATIN1 = "latin-1"
# Codec name prefixes: UTF-16/32 (only trusted from HTTP; a <meta> naming them
# is read as UTF-8 by HTML) and 7-bit stateful encodings.
_WIDE_PREFIXES = ("utf-16", "utf-32")
_SEVEN_BIT_PREFIXES = ("iso2022", "utf-7", "hz")
# WHATWG Encoding Standard: HTML reads these labels as their Windows supersets.
_WEB_ALIASES = {
    "ascii": "cp1252",
    "iso8859-1": "cp1252",
    "iso8859-9": "cp1254",
    "iso8859-11": "cp874",
    "tis-620": "cp874",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "shift_jis": "cp932",
    "euc_kr": "cp949",
    "big5": "big5hkscs",
}


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
    options.source = str(url.encode(_UTF8, "replace"), _UTF8)
    return options


# Comments and raw-text elements can hold <meta>-shaped text that is not markup.
_INACTIVE = re.compile(
    rb"<!--.*?(?:-->|$)|<(script|style|textarea|title|noscript|xmp)\b.*?(?:</\1\s*>|$)",
    re.DOTALL | re.IGNORECASE,
)
_META_TAG = re.compile(rb"<meta\s[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(rb"""([A-Za-z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""")
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
    attributes = {name.lower(): b"".join(value).lower() for name, *value in _ATTRIBUTE.findall(tag)}
    if b"charset" in attributes:
        return attributes[b"charset"]
    return _http_equiv_charset(attributes)


def _http_equiv_charset(attributes: dict[bytes, bytes]) -> bytes | None:
    content = attributes.get(b"content")
    if content is None or attributes.get(b"http-equiv") != b"content-type":
        return None
    match = _CONTENT_CHARSET.search(content)
    return match.group(1) if match else None


def _meta_charset(html: bytes) -> str | None:
    """Return the codec of the first active meta charset tag naming a known codec."""
    head = _INACTIVE.sub(b"", html[:4096])
    for tag in _META_TAG.findall(head):
        codec = _tag_codec(tag)
        if codec is not None:
            return codec
    return None


def _tag_codec(tag: bytes) -> str | None:
    declared = _tag_charset(tag)
    return None if declared is None else _codec(declared.decode(_LATIN1))


def _codec(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        codec = codecs.lookup(name).name
    except LookupError:
        return None
    return _WEB_ALIASES.get(codec, codec)


def _bom_codec(html: bytes) -> str | None:
    return next((codec for bom, codec in _BOMS if html.startswith(bom)), None)


def _ascii_lookalike(codec: str | None, prefixes: tuple[str, ...]) -> str | None:
    """Declared codec whose ASCII-only bytes also pass as valid UTF-8."""
    return codec if codec is not None and codec.startswith(prefixes) else None


def decode_html(html: bytes, charset: str | None = None) -> str:
    """Decode HTML bytes, preferring declared and UTF-8 codecs over detection.

    Strict decodes run in order: BOM, declared codecs whose bytes can look
    like ASCII (UTF-16/32 from HTTP, 7-bit ISO-2022/UTF-7/HZ from HTTP or
    meta), UTF-8, the HTTP charset, then ``<meta charset>``. If none fits, a page that is mostly
    UTF-8, or else one with a declared charset, keeps that codec and replaces
    only its bad bytes; detection is the last resort.
    """
    header, meta = _codec(charset), _meta_charset(html)
    declared = [header, meta]
    lookalikes = [
        _ascii_lookalike(header, _WIDE_PREFIXES + _SEVEN_BIT_PREFIXES),
        _ascii_lookalike(meta, _SEVEN_BIT_PREFIXES),
    ]
    strict = [_bom_codec(html), *lookalikes, _UTF8, *declared]
    decoded = _first_decoding(html, strict)
    if decoded is not None:
        return decoded
    lenient = _lenient_codec(html, declared)
    if lenient is not None:
        return str(html, lenient, "replace")
    return _first_decoding(html, detect_encoding(html)) or str(html, _UTF8, "replace")


def _first_decoding(html: bytes, encodings: Iterable[str | None]) -> str | None:
    for encoding in encodings:
        if encoding is None:
            continue
        try:
            return html.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def _lenient_codec(html: bytes, declared: list[str | None]) -> str | None:
    if _mostly_utf8(html):
        return _UTF8
    return next((codec for codec in declared if codec is not None), None)


def _mostly_utf8(html: bytes) -> bool:
    """More valid multi-byte UTF-8 characters than invalid sequences.

    Legacy single-byte text almost never forms valid multi-byte sequences.
    """
    text = str(html, _UTF8, "replace")
    invalid = text.count("\ufffd")
    return sum(1 for char in text if ord(char) > 127) - invalid > invalid


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
