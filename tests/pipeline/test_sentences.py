"""Language-gated sentence segmentation for one batch of polygon rows."""

from __future__ import annotations

import re
from collections.abc import Sequence

import pytest

from osm_polygon_website_tag.contracts.sentence_schema import (
    SENTENCE_ABSENT,
    SENTENCE_EMPTY_TEXT,
    SENTENCE_SUCCESS,
    SENTENCE_UNSUPPORTED_LANGUAGE,
)
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.pipeline.sentences import segment_batch, sentence_gate


class _FakeSplitter:
    """Deterministic splitter that records the batches it was handed."""

    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        self.batches: list[list[str]] = []
        self._mapping = mapping or {}

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity("repo", "file", "rev", "a" * 64)

    def split(self, texts: Sequence[str]) -> list[list[str]]:
        self.batches.append(list(texts))
        return [self._mapping.get(text, [text]) for text in texts]


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "website_text_status": "success",
        "website_text": "One. Two.",
        "website_language": "eng_Latn",
        "contact_website_text_status": "absent",
        "contact_website_text": None,
        "contact_website_language": None,
    }
    row.update(overrides)
    return row


def test_gate_defers_to_the_model_only_for_successful_supported_text() -> None:
    assert sentence_gate("hello", "eng_Latn") is None


def test_gate_marks_blank_text_empty_rather_than_segmenting_it() -> None:
    assert sentence_gate("   \n\t ", "eng_Latn") == SENTENCE_EMPTY_TEXT


def test_gate_records_a_language_the_model_does_not_cover() -> None:
    """GlotLID labels far more languages than the segmenter supports."""
    assert sentence_gate("hello", "zza_Latn") == SENTENCE_UNSUPPORTED_LANGUAGE
    assert sentence_gate("hello", None) == SENTENCE_UNSUPPORTED_LANGUAGE


def test_blank_text_is_checked_before_language_support() -> None:
    """An empty string is empty regardless of which language was detected."""
    assert sentence_gate("  ", "zza_Latn") == SENTENCE_EMPTY_TEXT


def test_segment_batch_fills_sentences_counts_and_status() -> None:
    splitter = _FakeSplitter({"One. Two.": ["One.", "Two."]})

    rows = segment_batch([_row()], splitter)

    assert rows[0]["website_sentences"] == ["One.", "Two."]
    assert rows[0]["website_sentence_count"] == 2
    assert rows[0]["website_sentence_status"] == SENTENCE_SUCCESS


def test_segment_batch_leaves_gated_rows_null_with_a_reason() -> None:
    splitter = _FakeSplitter()

    rows = segment_batch([_row(website_language="zza_Latn")], splitter)

    assert rows[0]["website_sentences"] is None
    assert rows[0]["website_sentence_count"] is None
    assert rows[0]["website_sentence_status"] == SENTENCE_UNSUPPORTED_LANGUAGE
    assert rows[0]["contact_website_sentence_status"] == SENTENCE_ABSENT
    assert splitter.batches == []


def test_segment_batch_sends_one_batched_call_per_website_field() -> None:
    """Texts are batched so the model runs once per field, not once per row."""
    splitter = _FakeSplitter()
    rows = [_row(website_text="a"), _row(website_text="b")]

    segment_batch(rows, splitter)

    assert splitter.batches == [["a", "b"]]


def test_segment_batch_handles_both_website_fields_independently() -> None:
    splitter = _FakeSplitter()

    rows = segment_batch(
        [
            _row(
                contact_website_text_status="success",
                contact_website_text="c",
                contact_website_language="fra_Latn",
            )
        ],
        splitter,
    )

    assert splitter.batches == [["One. Two."], ["c"]]
    assert rows[0]["contact_website_sentences"] == ["c"]
    assert rows[0]["contact_website_sentence_count"] == 1


def test_segment_batch_strips_and_drops_blank_model_output() -> None:
    splitter = _FakeSplitter({"One. Two.": ["  One.  ", "", "   ", "Two."]})

    rows = segment_batch([_row()], splitter)

    assert rows[0]["website_sentences"] == ["One.", "Two."]
    assert rows[0]["website_sentence_count"] == 2


def test_segment_batch_rejects_a_result_count_that_does_not_match_the_batch() -> None:
    class ShortSplitter(_FakeSplitter):
        def split(self, texts: Sequence[str]) -> list[list[str]]:
            del texts
            return []

    with pytest.raises(
        ValueError, match=rf"^{re.escape('sentence result count does not match input count')}$"
    ):
        segment_batch([_row()], ShortSplitter())


def test_segment_batch_rejects_empty_segmentation_of_non_empty_text() -> None:
    splitter = _FakeSplitter({"One. Two.": ["  ", ""]})

    with pytest.raises(
        ValueError, match=rf"^{re.escape('model returned no sentences for non-empty text')}$"
    ):
        segment_batch([_row()], splitter)


def test_segment_batch_rejects_a_non_string_successful_text() -> None:
    with pytest.raises(
        ValueError, match=rf"^{re.escape('successful website text is not a string')}$"
    ):
        segment_batch([_row(website_text=None)], _FakeSplitter())


def test_segment_batch_preserves_every_input_column() -> None:
    rows = segment_batch([_row(polygon_id="source:way/1")], _FakeSplitter())

    assert rows[0]["polygon_id"] == "source:way/1"
    assert rows[0]["website_text"] == "One. Two."
