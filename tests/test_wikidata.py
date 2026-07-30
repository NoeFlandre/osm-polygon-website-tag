"""Tests for Wikidata value classification."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.wikidata import (
    WikidataClass,
    classify_wikidata,
    extract_qid,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Q42", WikidataClass.CANONICAL_QID),
        ("Q123456789", WikidataClass.CANONICAL_QID),
        ("q42", WikidataClass.CANONICAL_QID),
        ("Q42;Q43", WikidataClass.MULTIPLE),
        ("Q42 , Q43", WikidataClass.MULTIPLE),
        ("Q42\nQ43", WikidataClass.MULTIPLE),
        ("Q42 Q43", WikidataClass.MULTIPLE),
        ("P42", WikidataClass.MALFORMED),
        ("Q", WikidataClass.MALFORMED),
        ("Q42x", WikidataClass.MALFORMED),
        ("foo", WikidataClass.MALFORMED),
        ("42", WikidataClass.MALFORMED),
        ("Q42;invalid", WikidataClass.MULTIPLE),
    ],
)
def test_classify_wikidata(value: str, expected: WikidataClass) -> None:
    assert classify_wikidata(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Q42", "Q42"),
        ("q42", "Q42"),
        ("Q123456789", "Q123456789"),
        ("Q42;Q43", "Q42"),
        ("Q42 , Q43", "Q42"),
        ("foo", None),
        ("P42", None),
    ],
)
def test_extract_qid(value: str, expected: str | None) -> None:
    assert extract_qid(value) == expected


def test_classify_wikidata_trims_whitespace() -> None:
    assert classify_wikidata("  Q42  ") == WikidataClass.CANONICAL_QID
