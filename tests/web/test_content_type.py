"""Content-Type parameter parsing shared by HTTP headers and meta tags."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.web.content_type import charset_parameter, media_type


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text/html; charset=utf-8", "utf-8"),
        ('text/html; charset="koi8-r"', "koi8-r"),
        ("text/html; charset='windows-874'", "windows-874"),
        ('text/html; note="; charset=koi8-r"; charset=windows-1251', "windows-1251"),
        ("text/html; note='; charset=koi8-r'; charset=windows-1251", "windows-1251"),
        ("text/html; xcharset=koi8-r", None),
        ("text/html; charset=", None),
        ("text/html", None),
        ("", None),
    ],
)
def test_charset_parameter(value: str, expected: str | None) -> None:
    assert charset_parameter(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Text/Plain ; charset=utf-8", "text/plain"),
        ("text/html", "text/html"),
        ("", None),
        ("; charset=utf-8", None),
    ],
)
def test_media_type(value: str, expected: str | None) -> None:
    assert media_type(value) == expected
