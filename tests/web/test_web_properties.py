"""Properties of URL handling and page decoding over untrusted input."""

from __future__ import annotations

import ipaddress
import urllib.parse

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from osm_polygon_website_tag.web import web_fetch
from osm_polygon_website_tag.web.text_extract import decode_html, extract_main_text
from osm_polygon_website_tag.web.web_fetch import (
    Resolver,
    UnsafeUrlError,
    normalize_http_url,
    validate_public_http_url,
)

_LABEL = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12)
_HOSTS = st.builds(".".join, st.lists(_LABEL, min_size=2, max_size=4))
_PATHS = st.text(alphabet="abcXYZ019/-_.~%", max_size=20)
_URLS = st.builds(
    lambda scheme, host, port, path: f"{scheme}{host}{port}/{path}",
    st.sampled_from(["", "http://", "https://", "//", "HTTP://", "Https://"]),
    _HOSTS,
    st.sampled_from(["", ":80", ":443", ":8080"]),
    _PATHS,
)
_RAW = st.one_of(_URLS, st.text(max_size=40))


def _accepted(raw: str) -> str | None:
    try:
        return normalize_http_url(raw)
    except ValueError:
        return None


@given(_RAW)
@example("exämple.org")
@example("http://a.b:80/")
@example("//a.b/c")
@example(".")
def test_normalising_twice_changes_nothing(raw: str) -> None:
    once = _accepted(raw)
    assume(once is not None)
    assert once is not None

    assert normalize_http_url(once) == once


@given(_RAW)
def test_an_accepted_url_is_http_or_https_with_a_plain_ascii_host_and_no_userinfo(raw: str) -> None:
    normalized = _accepted(raw)
    assume(normalized is not None)
    assert normalized is not None

    parsed = urllib.parse.urlsplit(normalized)

    assert parsed.scheme in {"http", "https"}
    assert parsed.hostname is not None
    assert parsed.hostname.isascii()
    assert parsed.hostname == parsed.hostname.lower()
    assert parsed.username is None
    assert parsed.password is None
    assert not parsed.fragment


@given(
    st.sampled_from(["http", "https"]),
    st.text(alphabet="abcxyz0123456789", min_size=1, max_size=8),
    st.one_of(st.none(), st.text(alphabet="abcxyz0123456789", min_size=1, max_size=8)),
    _HOSTS,
)
def test_urls_carrying_credentials_are_always_rejected(
    scheme: str, user: str, password: str | None, host: str
) -> None:
    userinfo = user if password is None else f"{user}:{password}"

    with pytest.raises(ValueError, match="credentials_not_allowed"):
        normalize_http_url(f"{scheme}://{userinfo}@{host}/")


_NON_PUBLIC_NETWORKS = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/29",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
]
_NON_PUBLIC = st.one_of(
    *(st.ip_addresses(network=network) for network in _NON_PUBLIC_NETWORKS),
    st.ip_addresses(v=4, network="10.0.0.0/8").map(lambda a: ipaddress.IPv6Address(f"::ffff:{a}")),
    st.ip_addresses(v=4, network="127.0.0.0/8").map(lambda a: ipaddress.IPv6Address(f"::ffff:{a}")),
    st.ip_addresses(v=4, network="192.168.0.0/16").map(
        lambda a: ipaddress.IPv6Address(f"::ffff:{a}")
    ),
)


@given(_NON_PUBLIC)
def test_no_non_public_address_is_treated_as_public(address: ipaddress._BaseAddress) -> None:
    assert isinstance(address, ipaddress.IPv4Address | ipaddress.IPv6Address)

    assert web_fetch._is_public_address(address) is False


def _resolver(*addresses: str) -> Resolver:
    def resolve(_host: str, port: int) -> list[tuple[object, ...]]:
        return [(2, 1, 6, "", (address, port)) for address in addresses]

    return resolve


@given(_NON_PUBLIC, st.lists(st.sampled_from(["93.184.216.34", "1.1.1.1"]), max_size=2), st.data())
def test_a_host_with_any_non_public_answer_is_never_accepted(
    address: ipaddress._BaseAddress, public: list[str], data: st.DataObject
) -> None:
    answers = [*public, str(address)]
    answers = data.draw(st.permutations(answers))

    with pytest.raises(UnsafeUrlError):
        validate_public_http_url("https://example.org/", resolver=_resolver(*answers))


@given(_NON_PUBLIC)
def test_a_literal_non_public_ip_is_never_accepted_without_resolving(
    address: ipaddress._BaseAddress,
) -> None:
    host = f"[{address}]" if address.version == 6 else str(address)

    def refuse(*_args: object) -> list[tuple[object, ...]]:
        raise AssertionError("a literal address must not be resolved")

    with pytest.raises(UnsafeUrlError):
        validate_public_http_url(f"http://{host}/", resolver=refuse)


@given(
    st.dictionaries(st.text(alphabet="abcXYZ-", min_size=1, max_size=8), st.text(max_size=8)),
    st.text(alphabet="abcXYZ-", min_size=1, max_size=8),
    st.data(),
)
def test_header_lookup_ignores_the_case_of_the_name(
    headers: dict[str, str], name: str, data: st.DataObject
) -> None:
    mangled = {
        "".join(data.draw(st.sampled_from([c.lower(), c.upper()])) for c in key): value
        for key, value in headers.items()
    }
    assume(len({key.lower() for key in mangled}) == len(mangled))

    assert web_fetch._header(mangled, name) == web_fetch._header(headers, name)


@given(st.binary(max_size=2000), st.one_of(st.none(), st.text(max_size=12)))
@example(b"", "\x00")
@example(b"<meta charset='\x00'>", None)
def test_extraction_never_raises_on_arbitrary_bytes(html: bytes, charset: str | None) -> None:
    result = extract_main_text(html, url="https://example.org/", charset=charset)

    assert result.status in {"success", "empty", "extract_error"}
    assert result.text is None or isinstance(result.text, str)


@given(st.binary(max_size=500), st.one_of(st.none(), st.text(max_size=12)))
@example(b"", "\x00")
@example(b"<meta charset='\x00'>", None)
def test_decoding_never_raises_and_always_returns_text(html: bytes, charset: str | None) -> None:
    assert isinstance(decode_html(html, charset), str)


_CODEC_ALPHABETS = {
    "utf-8": st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="<>&\"'"),
    "windows-1252": st.characters(
        min_codepoint=0xA0, max_codepoint=0xFF, blacklist_characters="<>&\"'"
    ),
    "shift_jis": st.sampled_from("こんにちは世界日本語テスト東京"),
    "euc-kr": st.sampled_from("안녕하세요세계한국어테스트서울"),
}


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@given(st.sampled_from(sorted(_CODEC_ALPHABETS)), st.data())
def test_a_page_declaring_its_meta_charset_round_trips(codec: str, data: st.DataObject) -> None:
    text = data.draw(st.text(_CODEC_ALPHABETS[codec], min_size=1, max_size=40))
    page = f'<meta charset="{codec}"><p>{text}</p>'.encode(codec)
    if codec == "windows-1252":
        # By design a single-byte label loses to UTF-8 when the bytes are valid
        # UTF-8 (mislabelled UTF-8 pages are far commoner than that accident).
        assume(not _is_utf8(page))

    assert f"<p>{text}</p>" in decode_html(page)
