"""Trafilatura adapter tests using static HTML only."""

from __future__ import annotations

import codecs
from typing import Any

import pytest

from osm_polygon_website_tag.web import encoding_labels, text_extract
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


def test_trafilatura_version_lookup_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_trafilatura_options_are_reused_per_thread(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_empty_trafilatura_result_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_extract.trafilatura, "extract", lambda *_args, **_kwargs: None)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.status == "empty"
    assert result.text == ""
    assert result.word_count == 0
    assert result.trafilatura_version == text_extract._trafilatura_version()


def test_extractor_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret response body")

    monkeypatch.setattr(text_extract.trafilatura, "extract", fail)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.status == "extract_error"
    assert result.text is None
    assert result.word_count is None
    assert result.message == "RuntimeError"


def test_full_text_is_retained_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_non_utf8_pages_decode_without_replacement_chars(
    encoding: str, sentence: str, head: str
) -> None:
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


def test_undecodable_page_falls_back_to_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_extract, "detect_encoding", lambda _html: ["no-such-codec", "ascii"])

    assert decode_html(b"caf\xe9") == "caf�"


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le"])
def test_declared_wide_unicode_charset_beats_utf8_fast_path(encoding: str) -> None:
    html = _page("plain ascii words " * 30).encode(encoding)

    assert decode_html(html, encoding) == _page("plain ascii words " * 30)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_byte_order_mark_selects_the_codec(encoding: str) -> None:
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


def test_extractor_options_drop_comments_keep_tables_and_sanitise_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_extract._extractor_state.__dict__.clear()
    constructed: list[dict[str, object]] = []
    real = text_extract.Extractor

    def record(**kwargs: Any) -> Any:
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


def test_meta_charset_is_only_read_from_the_first_1024_bytes() -> None:
    tag = b"<meta charset=koi8-r>"

    assert text_extract._meta_charset(b" " * (1024 - len(tag)) + tag) == "koi8-r"
    assert text_extract._meta_charset(b" " * (1025 - len(tag)) + tag) is None


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
def test_web_charset_labels_map_to_their_windows_supersets(label: str, codec: str) -> None:
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
def test_legacy_asian_labels_decode_extension_characters(
    label: str, codec: str, sentence: str
) -> None:
    html = _page(sentence * 20).encode(codec)

    assert sentence in decode_html(html, label)


def test_meta_content_charset_needs_a_parameter_boundary() -> None:
    tag = b"<meta http-equiv='Content-Type' content='text/html; xcharset=koi8-r; charset=windows-1252'>"

    assert text_extract._meta_charset(tag) == "cp1252"


@pytest.mark.parametrize("via_meta", [False, True])
def test_declared_cjk_codec_beats_coincidentally_valid_utf8(via_meta: bool) -> None:
    head = '<meta charset="gb2312">' if via_meta else ""
    text = _page("专业", head)
    html = text.encode("gbk")
    assert html.decode("utf-8")  # the GBK bytes are also valid UTF-8

    assert decode_html(html, None if via_meta else "gb2312") == text


def test_single_byte_label_does_not_override_valid_utf8() -> None:
    html = _page("Café crème. " * 10).encode("utf-8")

    assert decode_html(html, "windows-1252") == html.decode("utf-8")


def test_first_of_repeated_meta_attributes_wins() -> None:
    assert text_extract._meta_charset(b'<meta charset="koi8-r" charset="windows-1252">') == "koi8-r"


def test_first_decoding_skips_missing_and_failing_codecs() -> None:
    assert text_extract._first_decoding(b"\xff", [None, "ascii", "no-such-codec", "latin-1"]) == "ÿ"
    assert text_extract._first_decoding(b"\xff", ["ascii"]) is None


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (b"\x7f\x7f\xff", False),  # DEL is ASCII, not a multi-byte character
        ("\u0080".encode() * 2 + b"\xff", True),  # U+0080 is the first multi-byte one
    ],
)
def test_mostly_utf8_ascii_boundary(html: bytes, expected: bool) -> None:
    assert text_extract._mostly_utf8(html) is expected


def test_extractor_failure_reports_the_library_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(text_extract.trafilatura, "extract", fail)

    result = extract_main_text(b"<html/>", url="https://example.org")

    assert result.trafilatura_version == text_extract._trafilatura_version()


@pytest.mark.parametrize(
    ("label", "codec"),
    [
        ("windows-874", "cp874"),
        (" ISO-8859-8-I ", "iso8859-8"),
        ("x-sjis", "cp932"),
        ("x-cp1251", "cp1251"),
        ("x-mac-cyrillic", "mac-cyrillic"),
    ],
)
def test_web_only_labels_resolve_to_python_codecs(label: str, codec: str) -> None:
    assert text_extract._codec(label) == codec


def test_windows_874_declared_thai_page_decodes() -> None:
    text = _page("สวัสดีครับ")

    assert decode_html(text.encode("cp874"), "windows-874") == text


def test_bom_codec_survives_a_stray_bad_byte_despite_a_declaration() -> None:
    html = codecs.BOM_UTF8 + _page("Café crème. " * 10).encode() + b"\xff"

    decoded = decode_html(html, "windows-1252")

    assert decoded.startswith("<html>")
    assert "Café crème." in decoded
    assert decoded.endswith("�")


@pytest.mark.parametrize(
    "prefix",
    [
        b'<div title="<meta charset=koi8-r>"></div>',
        b"<a data-x='<meta charset=\"koi8-r\">'>x</a>",
        b"<metadata charset=koi8-r>",
    ],
)
def test_meta_text_inside_other_tags_is_ignored(prefix: bytes) -> None:
    assert text_extract._meta_charset(prefix + b"<meta charset=windows-1251>") == "cp1251"


@pytest.mark.parametrize(
    "prefix",
    [
        b'<div title="<script>"></div>',
        b'<div title="<!--"></div>',
        b"<meta data_charset=koi8-r>",
        b"<meta xml:charset=koi8-r>",
        b"<textarea><meta charset=koi8-r></textarea>",
    ],
)
def test_prescan_follows_html_tokenisation(prefix: bytes) -> None:
    assert text_extract._meta_charset(prefix + b'<meta charset="windows-1251">') == "cp1251"


def test_mismatched_end_tag_does_not_leave_text_only_mode() -> None:
    html = b"<title></textarea><meta charset=koi8-r></title><meta charset=windows-1251>"

    assert text_extract._meta_charset(html) == "cp1251"


def test_bare_charset_attribute_is_not_a_declaration() -> None:
    assert text_extract._meta_charset(b"<meta charset><meta charset=koi8-r>") == "koi8-r"


@pytest.mark.parametrize(("label", "codec"), [("windows-31j", "cp932"), ("Windows-949", "cp949")])
def test_windows_cjk_labels_resolve(label: str, codec: str) -> None:
    assert text_extract._codec(label) == codec


@pytest.mark.parametrize("label", ["utf-16le", "UTF-32"])
def test_wide_meta_labels_mean_utf8(label: str) -> None:
    assert text_extract._meta_charset(f'<meta charset="{label}">'.encode()) == "utf-8"


def test_meta_utf16_label_never_decodes_a_single_byte_page_as_utf16() -> None:
    html = _page("Café crème. " * 10, '<meta charset="utf-16le">').encode("cp1252")

    assert "Caf" in decode_html(html)
    assert "慃" not in decode_html(html)  # "Ca" read as one UTF-16LE unit


def test_http_charset_with_a_bad_byte_beats_a_conflicting_meta() -> None:
    html = _page("Café crème coûte 5€. " * 10, '<meta charset="windows-1252">').encode() + b"\xff"

    decoded = decode_html(html, "utf-8")

    assert "Café crème coûte 5€." in decoded
    assert decoded.endswith("�")


@pytest.mark.parametrize("label", ["ks_c_5601-1989", "csksc56011987", "cseuckr", "iso-ir-149"])
def test_euc_kr_web_labels_resolve_to_cp949(label: str) -> None:
    assert text_extract._codec(label) == "cp949"


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_charset_in_meta_content_is_read(quote: str) -> None:
    content = f"text/html; charset={quote}windows-874{quote}"
    q = "'" if quote == '"' else '"'
    tag = f"<meta http-equiv={q}Content-Type{q} content={q}{content}{q}>".encode()

    assert text_extract._meta_charset(tag) == "cp874"


def test_a_late_meta_after_1024_bytes_cannot_override_utf8() -> None:
    html = ("<p>" + "专业" * 400 + "</p><meta charset=gb2312>").encode()

    assert decode_html(html) == html.decode()


def test_plain_text_responses_do_not_sniff_meta() -> None:
    body = "<meta charset=gb2312> 专业 plain text".encode()

    result = extract_main_text(body, url="https://example.org", media_type="text/plain")

    assert text_extract.decode_html(body, sniff_meta=False) == body.decode()
    assert "涓" not in (result.text or "")  # GB18030 misreading of UTF-8 专


def test_html_media_type_still_sniffs_meta() -> None:
    html = _page("Москва большой город. " * 10, '<meta charset="windows-1251">').encode("cp1251")

    result = extract_main_text(html, url="https://example.org", media_type="text/html")

    assert "Москва" in (result.text or "")


def test_meta_content_skips_quoted_parameters() -> None:
    tag = (
        b'<meta http-equiv="Content-Type" '
        b"content='text/html; note=\"; charset=koi8-r\"; charset=windows-1251'>"
    )

    assert text_extract._meta_charset(tag) == "cp1251"


@pytest.mark.parametrize("label", ["x-x-big5", "cn-big5", "csbig5", "big5-hkscs"])
def test_big5_web_labels_resolve(label: str) -> None:
    assert text_extract._codec(label) == "big5hkscs"


def test_bare_charset_attribute_falls_through_to_http_equiv() -> None:
    tag = b'<meta charset http-equiv="Content-Type" content="text/html; charset=koi8-r">'

    assert text_extract._meta_charset(tag) == "koi8-r"


@pytest.mark.parametrize(
    "tag",
    [b'<meta content="text/html; charset=koi8-r">', b'<meta http-equiv="Content-Type">'],
)
def test_http_equiv_needs_both_attributes(tag: bytes) -> None:
    assert text_extract._meta_charset(tag) is None


def test_a_meta_charset_that_decodes_wins_over_a_mostly_utf8_body() -> None:
    # No HTTP charset: like browsers, a declaration that decodes is honoured.
    html = _page("Café " * 10, '<meta charset="windows-1252">').encode() + b"\xff"

    assert decode_html(html) == html.decode("cp1252")


# WHATWG Encoding Standard labels (encodings.json), minus UTF-16/32,
# ISO-2022-JP and "replacement", with the Python codec HTML should use.
_WHATWG_LABELS = (
    ("utf-8", "unicode-1-1-utf-8 unicode11utf8 unicode20utf8 utf-8 utf8 x-unicode20utf8"),
    ("cp866", "866 cp866 csibm866 ibm866"),
    (
        "iso8859-2",
        "csisolatin2 iso-8859-2 iso-ir-101 iso8859-2 iso88592 iso_8859-2 iso_8859-2:1987 l2 latin2",
    ),
    (
        "iso8859-3",
        "csisolatin3 iso-8859-3 iso-ir-109 iso8859-3 iso88593 iso_8859-3 iso_8859-3:1988 l3 latin3",
    ),
    (
        "iso8859-4",
        "csisolatin4 iso-8859-4 iso-ir-110 iso8859-4 iso88594 iso_8859-4 iso_8859-4:1988 l4 latin4",
    ),
    (
        "iso8859-5",
        "csisolatincyrillic cyrillic iso-8859-5 iso-ir-144 iso8859-5 iso88595 iso_8859-5 iso_8859-5:1988",
    ),
    (
        "iso8859-6",
        "arabic asmo-708 csiso88596e csiso88596i csisolatinarabic ecma-114 iso-8859-6 iso-8859-6-e iso-8859-6-i iso-ir-127 iso8859-6 iso88596 iso_8859-6 iso_8859-6:1987",
    ),
    (
        "iso8859-7",
        "csisolatingreek ecma-118 elot_928 greek greek8 iso-8859-7 iso-ir-126 iso8859-7 iso88597 iso_8859-7 iso_8859-7:1987 sun_eu_greek",
    ),
    (
        "iso8859-8",
        "csiso88598e csisolatinhebrew hebrew iso-8859-8 iso-8859-8-e iso-ir-138 iso8859-8 iso88598 iso_8859-8 iso_8859-8:1988 visual",
    ),
    ("iso8859-8", "csiso88598i iso-8859-8-i logical"),
    ("iso8859-10", "csisolatin6 iso-8859-10 iso-ir-157 iso8859-10 iso885910 l6 latin6"),
    ("iso8859-13", "iso-8859-13 iso8859-13 iso885913"),
    ("iso8859-14", "iso-8859-14 iso8859-14 iso885914"),
    ("iso8859-15", "csisolatin9 iso-8859-15 iso8859-15 iso885915 iso_8859-15 l9"),
    ("iso8859-16", "iso-8859-16"),
    ("koi8-r", "cskoi8r koi koi8 koi8-r koi8_r"),
    ("koi8-u", "koi8-ru koi8-u"),
    ("mac-roman", "csmacintosh mac macintosh x-mac-roman"),
    ("cp874", "dos-874 iso-8859-11 iso8859-11 iso885911 tis-620 windows-874"),
    (
        "cp1252",
        "ansi_x3.4-1968 ascii cp1252 cp819 csisolatin1 ibm819 iso-8859-1 iso-ir-100 iso8859-1 iso88591 iso_8859-1 iso_8859-1:1987 l1 latin1 us-ascii windows-1252 x-cp1252",
    ),
    (
        "cp1254",
        "cp1254 csisolatin5 iso-8859-9 iso-ir-148 iso8859-9 iso88599 iso_8859-9 iso_8859-9:1989 l5 latin5 windows-1254 x-cp1254",
    ),
    ("mac-cyrillic", "x-mac-cyrillic x-mac-ukrainian"),
    ("gb18030", "chinese csgb2312 csiso58gb231280 gb2312 gb_2312 gb_2312-80 gbk iso-ir-58 x-gbk"),
    ("gb18030", "gb18030"),
    ("big5hkscs", "big5 big5-hkscs cn-big5 csbig5 x-x-big5"),
    ("euc_jp", "cseucpkdfmtjapanese euc-jp x-euc-jp"),
    ("cp932", "csshiftjis ms932 ms_kanji shift-jis shift_jis sjis windows-31j x-sjis"),
    (
        "cp949",
        "cseuckr csksc56011987 euc-kr iso-ir-149 korean ks_c_5601-1987 ks_c_5601-1989 ksc5601 ksc_5601 windows-949",
    ),
    ("x-user-defined", "x-user-defined"),
    ("cp1250", "cp1250 windows-1250 x-cp1250"),
    ("cp1251", "cp1251 windows-1251 x-cp1251"),
    ("cp1253", "cp1253 windows-1253 x-cp1253"),
    ("cp1255", "cp1255 windows-1255 x-cp1255"),
    ("cp1256", "cp1256 windows-1256 x-cp1256"),
    ("cp1257", "cp1257 windows-1257 x-cp1257"),
    ("cp1258", "cp1258 windows-1258 x-cp1258"),
)


@pytest.mark.parametrize(
    ("label", "codec"),
    [(label, codec) for codec, labels in _WHATWG_LABELS for label in labels.split()],
)
def test_every_whatwg_label_resolves_to_its_web_codec(label: str, codec: str) -> None:
    assert text_extract._codec(f" {label.upper()} ") == codec


def test_the_label_table_holds_exactly_the_whatwg_labels() -> None:
    expected = {label for _codec, labels in _WHATWG_LABELS for label in labels.split()}

    assert set(encoding_labels.WEB_LABELS) == expected


def test_a_csgb2312_declaration_beats_a_valid_utf8_reading() -> None:
    assert decode_html("专业".encode("gb18030"), "csgb2312") == "专业"


def test_a_meta_inside_title_is_text_not_a_declaration() -> None:
    body = "<title><meta charset=koi8-r></title><p>Привет</p>".encode("cp1251")

    assert "Привет" in decode_html(body, "windows-1251")
    assert "Привет" in decode_html(
        b"<title><meta charset=koi8-r></title><meta charset=windows-1251>"
        + "<p>Привет</p>".encode("cp1251")
    )


def test_extract_main_text_sniffs_the_meta_charset_of_html() -> None:
    # GBK bytes for 专业 are also valid UTF-8, so only the <meta> gets this right.
    body = ("专业" * 60).encode("gbk")
    html = (
        b'<html><head><meta charset="gbk"></head><body><article><p>'
        + body
        + (b"</p></article></body></html>")
    )

    result = extract_main_text(html, url="https://example.org/", media_type="text/html")

    assert result.text is not None
    assert "专业专业" in result.text


def test_python_only_codec_names_still_get_their_web_superset() -> None:
    assert text_extract._codec("latin_1") == "cp1252"
    assert text_extract._codec("euckr") == "cp949"


def test_x_user_defined_from_http_maps_the_upper_half_to_the_private_use_area() -> None:
    assert decode_html(b"<p>a\x80\xff</p>", "x-user-defined") == "<p>a\uf780\uf7ff</p>"
    assert "a\uf780\uf7ff".encode("x-user-defined") == b"a\x80\xff"
    assert codecs.lookup("X-User-Defined").name == "x-user-defined"


def test_x_user_defined_codec_honours_the_error_handler() -> None:
    with pytest.raises(UnicodeEncodeError):
        "é".encode("x-user-defined")
    assert "é".encode("x-user-defined", "replace") == b"?"


def test_other_names_are_not_answered_by_the_x_user_defined_codec() -> None:
    with pytest.raises(LookupError):
        codecs.lookup("x-user-defined-extra")


def test_a_meta_x_user_defined_declaration_reads_as_windows_1252() -> None:
    """HTML's rule for <meta>: x-user-defined means windows-1252 there."""
    html = b"<meta charset=x-user-defined><p>price \x80</p>"

    assert decode_html(html).endswith("price \u20ac</p>")


def test_a_malformed_wide_http_charset_is_not_read_as_utf8() -> None:
    html = "<p>Hello</p>".encode("utf-16le") + b"X"

    assert decode_html(html, "utf-16le") == "<p>Hello</p>�"


def _gbk_meta_page() -> bytes:
    return '<meta charset="gb2312"/><article><p>专业</p></article>'.encode() * 40


@pytest.mark.parametrize(
    ("media_type", "sniffed"),
    [(None, True), ("text/html", True), ("application/xhtml+xml", False), ("text/plain", False)],
)
def test_only_html_responses_get_the_meta_prescan(
    monkeypatch: pytest.MonkeyPatch, media_type: str | None, sniffed: bool
) -> None:
    seen: list[bool] = []
    real = text_extract.decode_html

    def spy(html: bytes, charset: str | None = None, *, sniff_meta: bool = True) -> str:
        seen.append(sniff_meta)
        return real(html, charset, sniff_meta=sniff_meta)

    monkeypatch.setattr(text_extract, "decode_html", spy)

    text_extract.extract_main_text(_gbk_meta_page(), url="https://e.org/", media_type=media_type)

    assert seen == [sniffed]


def test_without_the_prescan_a_legacy_meta_does_not_override_utf8() -> None:
    page = _gbk_meta_page()

    assert "专业" in decode_html(page, None, sniff_meta=False)
    assert "专业" not in decode_html(page, None, sniff_meta=True)
