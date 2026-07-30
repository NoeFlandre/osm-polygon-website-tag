"""Tests for website classification and hostname extraction."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.website import (
    WebsiteClass,
    classify_contact_website,
    classify_website,
    extract_contact_hostname,
    extract_hostname,
    is_redacted,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com", WebsiteClass.ABSOLUTE_URL),
        ("http://example.com", WebsiteClass.ABSOLUTE_URL),
        ("HTTPS://Example.com", WebsiteClass.ABSOLUTE_URL),
        ("//example.com", WebsiteClass.SCHEME_RELATIVE),
        ("//cdn.example.com/path", WebsiteClass.SCHEME_RELATIVE),
        ("example.com", WebsiteClass.BARE_HOSTNAME),
        ("www.example.com", WebsiteClass.BARE_HOSTNAME),
        ("shop.example.co.uk", WebsiteClass.BARE_HOSTNAME),
        ("ftp://example.com", WebsiteClass.OTHER_SCHEME),
        ("mailto:foo@example.com", WebsiteClass.OTHER_SCHEME),
        ("https://example.com;https://other.com", WebsiteClass.MULTIPLE),
        ("https://example.com,https://other.com", WebsiteClass.MULTIPLE),
        ("https://example.com https://other.com", WebsiteClass.MULTIPLE),
        ("https://example.com\nhttps://other.com", WebsiteClass.MULTIPLE),
        ("!@#", WebsiteClass.MALFORMED),
        ("/", WebsiteClass.MALFORMED),
        ("no-tld-string", WebsiteClass.MALFORMED),
    ],
)
def test_classify_website(value: str, expected: WebsiteClass) -> None:
    assert classify_website(value) == expected


def test_classify_contact_website_is_alias_for_classify_website() -> None:
    """``classify_contact_website`` and ``classify_website`` must agree
    on identical input. The distinction is in *which* key the value
    came from, not in the value's classification."""
    for v in [
        "https://example.com",
        "example.com",
        "//example.com",
        "not-a-url",
        "",
    ]:
        assert classify_contact_website(v) == classify_website(v)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com", "example.com"),
        ("http://example.com", "example.com"),
        ("HTTPS://Example.COM", "example.com"),
        ("//example.com", "example.com"),
        ("//EXAMPLE.com/path", "example.com"),
        ("example.com", "example.com"),
        ("www.example.com", "www.example.com"),
        ("shop.example.co.uk", "shop.example.co.uk"),
        ("https://www.example.com/path/here", "www.example.com"),
        ("https://example.com:8080", "example.com"),
        ("no-tld", None),
        ("/", None),
    ],
)
def test_extract_hostname(value: str, expected: str | None) -> None:
    assert extract_hostname(value) == expected


def test_extract_contact_hostname_is_alias_for_extract_hostname() -> None:
    for v, expected in [
        ("https://example.com", "example.com"),
        ("example.com", "example.com"),
        ("//foo.com/path", "foo.com"),
        ("no-tld", None),
    ]:
        assert extract_contact_hostname(v) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", True),
        ("   ", True),
        ("https://user@example.com", True),
        ("https://example.com/?ref=abc", True),
        ("https://example.com/path?a=1&b=2", True),
        ("https://example.com/#section", True),
        ("https://example.com", False),
        ("https://example.com/path", False),
        ("https://example.com:8080", False),
        ("//example.com:8080", False),
        ("example.com", False),
    ],
)
def test_is_redacted(value: str, expected: bool) -> None:
    assert is_redacted(value) == expected


def test_classify_website_is_pure() -> None:
    v = "https://example.com"
    assert classify_website(v) == classify_website(v) == WebsiteClass.ABSOLUTE_URL


def test_extract_hostname_strips_path_and_query() -> None:
    assert extract_hostname("https://example.com/page?x=1&y=2#frag") == "example.com"
