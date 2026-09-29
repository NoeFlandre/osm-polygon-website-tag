"""Micro-benchmarks for the per-page hot paths (URL checks and text extraction).

Run with ``just bench``. The default test run does not collect this directory.
"""

from __future__ import annotations

import pytest
from pytest_benchmark.fixture import BenchmarkFixture

from osm_polygon_website_tag.web.text_extract import extract_main_text
from osm_polygon_website_tag.web.web_fetch import normalize_http_url, validate_public_http_url

_PARAGRAPH = "<p>Le musée est ouvert tous les jours, de neuf heures à dix-sept heures.</p>"


def _page(paragraphs: int) -> bytes:
    body = _PARAGRAPH * paragraphs
    return f"<html><head><title>Musée</title></head><body><article>{body}</article></body></html>".encode()


def _public_resolver(*_: object, **__: object) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


@pytest.mark.parametrize("paragraphs", [10, 300])
def test_extract_main_text(benchmark: BenchmarkFixture, paragraphs: int) -> None:
    html = _page(paragraphs)
    # The first call pays the lazy trafilatura import; time only steady state.
    extract_main_text(html, url="https://example.org/")

    result = benchmark(extract_main_text, html, url="https://example.org/")

    assert result.status == "success"


def test_normalize_http_url(benchmark: BenchmarkFixture) -> None:
    assert benchmark(normalize_http_url, "HTTP://Exämple.org:80/a b?q=1#frag")


def test_validate_public_http_url(benchmark: BenchmarkFixture) -> None:
    assert benchmark(validate_public_http_url, "https://example.org/", resolver=_public_resolver)
