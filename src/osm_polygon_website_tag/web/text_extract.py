"""Small deterministic Trafilatura adapter for downloaded HTML."""

from __future__ import annotations

import codecs
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from importlib.metadata import version
from typing import Literal

import trafilatura
from trafilatura.settings import Extractor
from trafilatura.utils import detect_encoding

from osm_polygon_website_tag.contracts.text_schema import count_words
from osm_polygon_website_tag.web.content_type import charset_parameter
from osm_polygon_website_tag.web.encoding_labels import WEB_LABELS, X_USER_DEFINED


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
_WINDOWS_1252 = "cp1252"
_REPLACEMENT = "\ufffd"
# Codec name prefixes: UTF-16/32 (only trusted from HTTP; a <meta> naming them
# is read as UTF-8 by HTML) and 7-bit stateful encodings.
_WIDE_PREFIXES = ("utf-16", "utf-32")
_SEVEN_BIT_PREFIXES = ("iso2022", "utf-7", "hz")
# Declared multi-byte CJK codecs also beat UTF-8: short CJK text in them can
# happen to be valid UTF-8 (GBK "专业" is d7 a8 d2 b5). Single-byte labels do
# not, because mislabelled UTF-8 pages are common and cp1252 text is rarely
# valid UTF-8 by accident.
_MULTIBYTE_PREFIXES = ("gb18030", "cp932", "cp949", "big5", "euc_j", "shift_jis")
# Python names outside the WHATWG table that HTML reads as Windows supersets.
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


# The HTML meta prescan applies to HTML only: in plain text a <meta> is literal
# text, and XHTML is XML, which a <meta> does not declare. An unknown type is
# treated as HTML.
_META_SNIFFED_TYPES = (None, "text/html")
# HTML only honours encoding declarations within the first 1024 bytes.
_PRESCAN_BYTES = 1024
# html.parser already treats script/style as raw text; these hold text, not
# markup, too, so a <meta> inside them is ignored.
_TEXT_ONLY_ELEMENTS = frozenset({"title", "textarea", "noscript", "xmp"})
# Byte-order marks win over any declaration, as in the WHATWG sniffing algorithm.
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class _MetaCharsetParser(HTMLParser):
    """Collect charset declarations from real ``<meta>`` tags, in document order.

    The standard parser handles quoted attributes, comments, full attribute
    names and script/style content, so meta-shaped text there is not a tag.
    """

    def __init__(self) -> None:
        super().__init__()
        self.declared: list[str] = []
        self._text_only: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._text_only is not None:
            return
        if tag in _TEXT_ONLY_ELEMENTS:
            self._text_only = tag
        elif tag == "meta":
            self._record(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._text_only:
            self._text_only = None

    def _record(self, attrs: list[tuple[str, str | None]]) -> None:
        # dict(reversed(...)) keeps the first of repeated attributes, as HTML does.
        attributes = dict(reversed([(name, value or "") for name, value in attrs]))
        declared = attributes.get("charset") or _http_equiv_charset(attributes)
        if declared:
            self.declared.append(declared)


def _http_equiv_charset(attributes: dict[str, str]) -> str | None:
    http_equiv, content = attributes.get("http-equiv"), attributes.get("content")
    if http_equiv is None or content is None or http_equiv.lower() != "content-type":
        return None
    return charset_parameter(content)


def _meta_charset(html: bytes) -> str | None:
    """Return the codec of the first real meta charset declaration naming a known codec."""
    parser = _MetaCharsetParser()
    parser.feed(html[:_PRESCAN_BYTES].decode(_LATIN1))
    return next(filter(None, map(_meta_codec, parser.declared)), None)


def _meta_codec(label: str) -> str | None:
    """Apply HTML's <meta> overrides: UTF-16/32 reads as UTF-8 (the page is
    already ASCII-readable) and x-user-defined as windows-1252."""
    codec = _codec(label)
    if codec == X_USER_DEFINED:
        return _WINDOWS_1252
    return _UTF8 if codec is not None and codec.startswith(_WIDE_PREFIXES) else codec


def _codec(name: str | None) -> str | None:
    if name is None:
        return None
    label = name.strip().lower()
    try:
        codec = codecs.lookup(WEB_LABELS.get(label, label)).name
    except LookupError:
        return None
    return _WEB_ALIASES.get(codec, codec)


def _bom_codec(html: bytes) -> str | None:
    return next((codec for bom, codec in _BOMS if html.startswith(bom)), None)


def _ascii_lookalike(codec: str | None, prefixes: tuple[str, ...]) -> str | None:
    """Declared codec whose ASCII-only bytes also pass as valid UTF-8."""
    return codec if codec is not None and codec.startswith(prefixes) else None


def decode_html(html: bytes, charset: str | None = None, *, sniff_meta: bool = True) -> str:
    """Decode HTML bytes, preferring declared and UTF-8 codecs over detection.

    Order: a BOM decides outright, then a UTF-16/32 HTTP charset (bad bytes
    replaced). Then strict decodes of declared codecs whose bytes can pass as
    UTF-8 (7-bit ISO-2022/UTF-7/HZ and multi-byte CJK from HTTP or meta),
    UTF-8, and the HTTP charset. An
    HTTP charset that still fails keeps its codec with bad bytes replaced,
    before ``<meta charset>`` is considered at all. ``sniff_meta=False``
    (plain-text and XHTML responses) skips the HTML meta scan.
    """
    bom = _bom_codec(html)
    if bom is not None:  # a byte-order mark is authoritative; only bad bytes are replaced
        return str(html, bom, "replace")
    header = _codec(charset)
    wide = _ascii_lookalike(header, _WIDE_PREFIXES)
    if wide is not None:
        # Declared UTF-16/32 is authoritative even when malformed: its ASCII
        # text, NULs included, would otherwise pass as UTF-8.
        return str(html, wide, "replace")
    meta = _meta_charset(html) if sniff_meta else None
    lookalikes = [
        _ascii_lookalike(header, _SEVEN_BIT_PREFIXES + _MULTIBYTE_PREFIXES),
        _ascii_lookalike(meta, _SEVEN_BIT_PREFIXES + _MULTIBYTE_PREFIXES),
    ]
    decoded = _first_decoding(html, [*lookalikes, _UTF8, header])
    if decoded is not None:
        return decoded
    if header is not None:  # the HTTP charset outranks meta
        return str(html, header, "replace")
    return _decode_by_meta(html, meta)


def _decode_by_meta(html: bytes, meta: str | None) -> str:
    """No usable HTTP charset: strict meta, then a lenient codec, then detection."""
    decoded = _first_decoding(html, [meta])
    if decoded is not None:
        return decoded
    lenient = _lenient_codec(html, meta)
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


def _lenient_codec(html: bytes, meta: str | None) -> str | None:
    return _UTF8 if _mostly_utf8(html) else meta


def _mostly_utf8(html: bytes) -> bool:
    """More valid multi-byte UTF-8 characters than invalid sequences.

    Legacy single-byte text almost never forms valid multi-byte sequences.
    """
    text = str(html, _UTF8, "replace")
    invalid = text.count(_REPLACEMENT)
    return sum(1 for char in text if ord(char) > 127) - invalid > invalid


def extract_main_text(
    html: bytes, *, url: str, charset: str | None = None, media_type: str | None = None
) -> TextExtraction:
    """Extract full main text from already downloaded HTML (or plain text)."""
    library_version = _trafilatura_version()
    decoded = decode_html(html, charset, sniff_meta=media_type in _META_SNIFFED_TYPES)
    try:
        # An Extractor ``options`` object overrides trafilatura's per-call
        # keyword settings, so the URL and output settings live only there.
        value = trafilatura.extract(decoded, options=_extractor_options(url))
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
