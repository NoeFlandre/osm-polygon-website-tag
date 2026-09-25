"""Contract mapping GlotLID labels onto the segmenter's supported languages."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from scripts.quality.wtpsplit_languages import FIXTURE, render_snapshot

from osm_polygon_website_tag.pipeline.sentence_languages import (
    SAT_LANGUAGE_CODES,
    SUPPORTED_GLOTLID_SUBTAGS,
    sat_code_for_glotlid_label,
)

ROOT = Path(__file__).resolve().parents[2]


def _snapshot() -> tuple[str, frozenset[str]]:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    return lines[0].removeprefix("# wtpsplit=="), frozenset(lines[1:])


def test_supported_codes_match_the_wtpsplit_metadata_snapshot() -> None:
    """The pinned table must not drift from what wtpsplit actually ships.

    wtpsplit is in the optional `sentences` extra, so the default suite reads
    its `Constants.LANGINFO.index` from a snapshot written by
    `scripts/quality/wtpsplit_languages.py`.
    """
    _, codes = _snapshot()

    assert codes == SAT_LANGUAGE_CODES


def test_the_wtpsplit_snapshot_matches_the_locked_version() -> None:
    """A pin bump must regenerate the snapshot, or the contract above is stale."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    version, _ = _snapshot()

    assert version == locked["wtpsplit"]


def test_the_snapshot_script_renders_sorted_unique_codes() -> None:
    csv_text = ",ud,opus100\nfr,UD_French,fr-en\naf,UD_Afrikaans,af-en\nfr,x,y\n\n"

    assert render_snapshot("9.9", csv_text) == "# wtpsplit==9.9\naf\nfr\n"


def test_every_supported_code_is_reachable_from_at_least_one_subtag() -> None:
    """Alternative ISO 639-3 subtags are intentional; every code stays reachable."""
    reached = {sat_code_for_glotlid_label(subtag) for subtag in SUPPORTED_GLOTLID_SUBTAGS}

    assert reached == set(SAT_LANGUAGE_CODES)


def test_no_subtag_resolves_ambiguously_to_two_codes() -> None:
    pairs = [(subtag, sat_code_for_glotlid_label(subtag)) for subtag in SUPPORTED_GLOTLID_SUBTAGS]

    assert len({subtag for subtag, _ in pairs}) == len(pairs)


def test_glotlid_labels_resolve_by_language_ignoring_script() -> None:
    """GlotLID scripts a label; the segmenter is script-agnostic."""
    assert sat_code_for_glotlid_label("eng_Latn") == "en"
    assert sat_code_for_glotlid_label("srp_Cyrl") == "sr"
    assert sat_code_for_glotlid_label("srp_Latn") == "sr"
    assert sat_code_for_glotlid_label("cmn_Hani") == "zh"
    # Only the first underscore separates the subtag; the rest is script detail.
    assert sat_code_for_glotlid_label("eng_Latn_XX") == "en"


def test_unsupported_and_malformed_labels_resolve_to_nothing() -> None:
    assert sat_code_for_glotlid_label("zza_Latn") is None
    assert sat_code_for_glotlid_label(None) is None
    assert sat_code_for_glotlid_label("") is None
    assert sat_code_for_glotlid_label(17) is None
    assert sat_code_for_glotlid_label("_Latn") is None


def test_a_bare_subtag_without_a_script_still_resolves() -> None:
    assert sat_code_for_glotlid_label("fra") == "fr"


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("deu_Latn", "de"),
        ("nld_Latn", "nl"),
        ("ell_Grek", "el"),
        ("heb_Hebr", "he"),
        ("jpn_Jpan", "ja"),
        ("kor_Hang", "ko"),
        ("zsm_Latn", "ms"),
        ("nob_Latn", "no"),
        ("pes_Arab", "fa"),
    ],
)
def test_common_three_letter_subtags_map_to_their_two_letter_code(label: str, code: str) -> None:
    assert sat_code_for_glotlid_label(label) == code
