"""Trafilatura adapter tests using static HTML only."""

from __future__ import annotations

from typing import Any

import pytest

from osm_polygon_website_tag.web import text_extract
from osm_polygon_website_tag.web.text_extract import decode_html, extract_main_text


def test_extract_main_text_from_static_html() -> None:
    html = b"""
    <html><body>
      <nav>Navigation noise</nav>
      <main><article><h1>Public Library</h1>
      <p>This library serves the whole community with books and archives.</p>
      </article></main>
      <div class="comments">A visitor comment that must not be included.</div>
    </body></html>
    """

    result = extract_main_text(html, url="https://example.org/library")

    assert result.status == "success"
    assert result.text is not None
    assert "Public Library" in result.text
    assert "serves the whole community" in result.text
    assert result.word_count is not None
    assert result.word_count > 0


def test_trafilatura_version_lookup_is_cached(monkeypatch) -> None:
    """Repeated URL extraction must not rescan package metadata each time."""
    calls: list[str] = []

    def fake_version(name: str) -> str:
        calls.append(name)
        return "test-version"

    cached_version = getattr(text_extract, "_trafilatura_version", None)
    if cached_version is not None:
        cached_version.cache_clear()
    monkeypatch.setattr(text_extract, "version", fake_version)
    monkeypatch.setattr(text_extract.trafilatura, "extract", lambda *_args, **_kwargs: "text")
    try:
        first = extract_main_text(b"<html/>", url="https://example.org/one")
        second = extract_main_text(b"<html/>", url="https://example.org/two")
    finally:
        if cached_version is not None:
            cached_version.cache_clear()

    assert first.trafilatura_version == "test-version"
    assert second.trafilatura_version == "test-version"
    assert calls == ["trafilatura"]


def test_trafilatura_options_are_reused_per_thread(monkeypatch) -> None:
    """Repeated extraction reuses setup while updating the current URL."""
    state = getattr(text_extract, "_extractor_state", None)
    if state is not None:
        state.__dict__.clear()
    constructions: list[dict[str, object]] = []

    class FakeExtractor:
        def __init__(self, **kwargs: object) -> None:
            constructions.append(kwargs)
            self.url = kwargs.get("url")
            self.source = kwargs.get("url")

    seen_options: list[FakeExtractor] = []

    def fake_extract(*_args: object, **kwargs: object) -> str:
        option = kwargs["options"]
        assert isinstance(option, FakeExtractor)
        seen_options.append(option)
        return "text"

    monkeypatch.setattr(text_extract, "Extractor", FakeExtractor, raising=False)
    monkeypatch.setattr(text_extract.trafilatura, "extract", fake_extract)
    try:
        first = extract_main_text(b"<html/>", url="https://example.org/one")
        second = extract_main_text(b"<html/>", url="https://example.org/two")
    finally:
        if state is not None:
            state.__dict__.clear()

    assert first.text == second.text == "text"
    assert len(constructions) == 1
    assert seen_options[0] is seen_options[1]
    assert seen_options[1].url == "https://example.org/two"
    assert seen_options[1].source == "https://example.org/two"


def test_empty_trafilatura_result_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(text_extract.trafilatura, "extract", lambda *_args, **_kwargs: None)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.status == "empty"
    assert result.text == ""
    assert result.word_count == 0


def test_extractor_failure_is_sanitized(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("secret response body")

    monkeypatch.setattr(text_extract.trafilatura, "extract", fail)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.status == "extract_error"
    assert result.text is None
    assert result.word_count is None
    assert result.message == "RuntimeError"


def test_full_text_is_retained_without_truncation(monkeypatch) -> None:
    full = "word " * 1_000_000
    monkeypatch.setattr(text_extract.trafilatura, "extract", lambda *_args, **_kwargs: full)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.text == full
    assert result.word_count == 1_000_000


def _page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body><article><p>{body}</p></article></body></html>"


def test_http_charset_decodes_unlabelled_cp1252_page() -> None:
    html = _page("Café crème à Paris près de la rivière. " * 30).encode("cp1252")

    result = extract_main_text(html, url="https://example.org", charset="windows-1252")

    assert "\ufffd" not in (result.text or "")
    assert "Café crème à Paris" in (result.text or "")


def test_invalid_http_charset_falls_back_to_detection() -> None:
    html = _page("Москва большой красивый город. " * 30, '<meta charset="koi8-r">').encode("koi8-r")

    assert "Москва" in decode_html(html, "no-such-codec")


@pytest.mark.parametrize(
    ("encoding", "sentence", "head"),
    [
        ("cp1252", "Café crème à Paris près de la rivière. ", '<meta charset="windows-1252">'),
        ("shift_jis", "東京の公園はとても美しい場所です。", '<meta charset="shift_jis">'),
        (
            "koi8-r",
            "Москва большой красивый город. ",
            '<meta http-equiv="Content-Type" content="text/html; charset=koi8-r">',
        ),
    ],
)
def test_non_utf8_pages_decode_without_replacement_chars(encoding, sentence, head) -> None:
    html = _page(sentence * 30, head).encode(encoding)

    result = extract_main_text(html, url="https://example.org")

    assert result.status == "success"
    assert "�" not in (result.text or "")
    assert sentence.strip()[:8] in (result.text or "")


def test_utf8_page_decodes_as_before() -> None:
    html = _page("Café crème à Paris. " * 30).encode("utf-8")

    assert decode_html(html) == html.decode("utf-8", errors="replace")


def test_unknown_meta_charset_is_ignored() -> None:
    html = _page("Café crème. " * 30, '<meta charset="no-such-codec">').encode("cp1252")

    assert "�" not in decode_html(html)


def test_undecodable_page_falls_back_to_replacement(monkeypatch) -> None:
    monkeypatch.setattr(text_extract, "detect_encoding", lambda _html: ["no-such-codec", "ascii"])

    assert decode_html(b"caf\xe9") == "caf�"


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le"])
def test_declared_wide_unicode_charset_beats_utf8_fast_path(encoding) -> None:
    html = _page("plain ascii words " * 30).encode(encoding)

    assert decode_html(html, encoding) == _page("plain ascii words " * 30)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_byte_order_mark_selects_the_codec(encoding) -> None:
    html = _page("Café crème. " * 30).encode(encoding)

    assert decode_html(html, "iso-8859-1") == _page("Café crème. " * 30)


def test_commented_out_meta_charset_is_ignored() -> None:
    head = '<!-- <meta charset="iso-8859-1"> --><meta charset="koi8-r">'
    html = _page("Москва большой красивый город. " * 30, head).encode("koi8-r")

    assert "Москва" in decode_html(html)


def test_unrelated_meta_content_mentioning_charset_is_ignored() -> None:
    head = '<meta name="description" content="set charset=iso-8859-1 here">'
    head += '<meta http-equiv="Content-Type" content="text/html; charset=koi8-r">'
    html = _page("Москва большой красивый город. " * 30, head).encode("koi8-r")

    assert "Москва" in decode_html(html)


def test_meta_without_charset_declaration_yields_none() -> None:
    assert text_extract._meta_charset(b'<meta http-equiv="refresh" content="0">') is None
    assert (
        text_extract._meta_charset(b'<meta http-equiv="Content-Type" content="text/html">') is None
    )


def test_extractor_options_drop_comments_keep_tables_and_sanitise_url(monkeypatch) -> None:
    text_extract._extractor_state.__dict__.clear()
    constructed: list[dict[str, object]] = []
    real = text_extract.Extractor

    def record(**kwargs: Any):
        constructed.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(text_extract, "Extractor", record)
    try:
        options = text_extract._extractor_options("https://example.org/\udcff")
    finally:
        text_extract._extractor_state.__dict__.clear()

    assert constructed == [{"output_format": "txt", "comments": False, "tables": True}]
    assert options.url == "https://example.org/\udcff"
    assert options.source == "https://example.org/?"


def test_non_ascii_meta_charset_is_ignored() -> None:
    assert text_extract._meta_charset(b'<meta charset="\xe9">') is None


def test_comment_inside_meta_tag_is_removed_before_parsing() -> None:
    assert text_extract._meta_charset(b"<meta charset=koi8-r<!-- x -->>") == "koi8-r"


def test_meta_charset_is_only_read_from_the_first_4096_bytes() -> None:
    tag = b"<meta charset=koi8-r>"

    assert text_extract._meta_charset(b" " * (4096 - len(tag)) + tag) == "koi8-r"
    assert text_extract._meta_charset(b" " * (4097 - len(tag)) + tag) is None


@pytest.mark.parametrize(
    "tag",
    [
        b'<meta http-equiv="Content-Type">',
        b'<meta content="text/html; charset=koi8-r">',
        b'<meta http-equiv="refresh" content="0; charset=koi8-r">',
    ],
)
def test_incomplete_http_equiv_declarations_yield_none(tag: bytes) -> None:
    assert text_extract._meta_charset(tag) is None


def test_single_quoted_http_equiv_charset_is_read() -> None:
    tag = b"<META HTTP-EQUIV='content-type' CONTENT='text/html; Charset=KOI8-R'>"

    assert text_extract._meta_charset(tag) == "koi8-r"


def test_invalid_meta_charset_does_not_hide_a_later_valid_one() -> None:
    head = b'<meta charset="no-such-codec"><meta charset="windows-1251">'

    assert text_extract._meta_charset(head) == "cp1251"


@pytest.mark.parametrize(
    ("label", "codec"),
    [
        ("iso-8859-1", "cp1252"),
        ("latin1", "cp1252"),
        ("us-ascii", "cp1252"),
        ("iso-8859-9", "cp1254"),
        ("iso-8859-11", "cp874"),
        ("tis-620", "cp874"),
        ("gb2312", "gb18030"),
        ("GBK", "gb18030"),
        ("Shift_JIS", "cp932"),
        ("euc-kr", "cp949"),
        ("big5", "big5hkscs"),
        ("koi8-r", "koi8-r"),
    ],
)
def test_web_charset_labels_map_to_their_windows_supersets(label, codec) -> None:
    assert text_extract._codec(label) == codec


def test_latin1_label_decodes_windows_1252_punctuation() -> None:
    html = _page("“Café” costs 5€. " * 30).encode("cp1252")

    assert "“Café” costs 5€." in decode_html(html, "iso-8859-1")


def test_isolated_bad_byte_in_utf8_page_keeps_utf8() -> None:
    html = _page("Café crème coûte 5€. " * 30).encode("utf-8") + b"\xff"

    decoded = decode_html(html)

    assert "Café crème coûte 5€." in decoded
    assert decoded.endswith("�")


def test_isolated_bad_byte_keeps_the_declared_charset() -> None:
    body = _page("Москва большой город. " * 30, '<meta charset="windows-1251">')
    html = body.encode("cp1251") + b"\x98"  # 0x98 is undefined in cp1251

    decoded = decode_html(html)

    assert "Москва большой город." in decoded
    assert decoded.endswith("�")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("é€".encode() + b"\xff", True),
        ("é".encode() + b"\xff", False),
        (b"caf\xe9 cr\xe8me", False),
        (b"plain ascii", False),
    ],
)
def test_mostly_utf8_counts_valid_against_invalid_sequences(html: bytes, expected: bool) -> None:
    assert text_extract._mostly_utf8(html) is expected


@pytest.mark.parametrize("label", ["iso-2022-jp", "utf-7"])
def test_declared_seven_bit_codec_beats_utf8(label: str) -> None:
    text = _page("東京の公園はとても美しい場所です。" * 10)

    assert decode_html(text.encode(label), label) == text


def test_seven_bit_codec_declared_in_meta_beats_utf8() -> None:
    text = _page("東京の公園はとても美しい場所です。" * 10, '<meta charset="iso-2022-jp">')

    assert decode_html(text.encode("iso-2022-jp")) == text


def test_utf16_declared_in_meta_does_not_override_utf8() -> None:
    html = _page("plain words " * 10, '<meta charset="utf-16">').encode()

    assert decode_html(html) == html.decode()


@pytest.mark.parametrize(
    "inactive",
    [
        "<script>const t = '<meta charset=\"koi8-r\">';</script>",
        "<STYLE>/* <meta charset=koi8-r> */</STYLE >",
        "<title><meta charset=koi8-r></title>",
    ],
)
def test_meta_text_inside_raw_text_elements_is_ignored(inactive: str) -> None:
    head = f'{inactive}<meta charset="windows-1251">'.encode()

    assert text_extract._meta_charset(head) == "cp1251"


def test_unclosed_script_hides_everything_after_it() -> None:
    assert text_extract._meta_charset(b"<script><meta charset=koi8-r>") is None


@pytest.mark.parametrize(
    ("label", "codec", "sentence"),
    [
        ("gb2312", "gbk", "镕基在北京开会。"),
        ("Shift_JIS", "cp932", "①番目の東京の公園です。"),
    ],
)
def test_legacy_asian_labels_decode_extension_characters(label, codec, sentence) -> None:
    html = _page(sentence * 20).encode(codec)

    assert sentence in decode_html(html, label)
