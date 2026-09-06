"""Language-gated sentence segmentation for one batch of polygon rows.

Segmentation is attempted only for text the fetch stage completed, that is
non-blank, and whose detected language the model actually covers.  Every other
row records why it was skipped instead of carrying an indistinguishable null.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from osm_polygon_website_tag.contracts.sentence_schema import (
    SENTENCE_ABSENT,
    SENTENCE_EMPTY_TEXT,
    SENTENCE_SUCCESS,
    SENTENCE_UNSUPPORTED_LANGUAGE,
)
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.pipeline.sentence_languages import sat_code_for_glotlid_label

TEXT_PREFIXES = ("website", "contact_website")

_Pending = tuple[dict[str, object], str]


class SentenceSplitter(Protocol):
    """Small segmentation boundary consumed by the shard pipeline."""

    @property
    def identity(self) -> ModelIdentity: ...

    def split(self, texts: Sequence[str]) -> list[list[str]]: ...


def sentence_gate(prefix: str, text_status: object, text: object, language: object) -> str | None:
    """Return the terminal status for a row, or ``None`` to segment it.

    Blankness is decided before language support so that empty text is reported
    as empty whatever the detector labelled it.
    """
    if text_status != "success":
        return SENTENCE_ABSENT
    if not isinstance(text, str):
        raise ValueError(f"successful {prefix} text is not a string")
    if not text.strip():
        return SENTENCE_EMPTY_TEXT
    if sat_code_for_glotlid_label(language) is None:
        return SENTENCE_UNSUPPORTED_LANGUAGE
    return None


def segment_batch(
    originals: list[dict[str, object]], splitter: SentenceSplitter
) -> list[dict[str, object]]:
    """Segment one batch, calling the model once per website field."""
    pending: dict[str, list[_Pending]] = {prefix: [] for prefix in TEXT_PREFIXES}
    rows = [_prepare_row(original, pending) for original in originals]
    for prefix in TEXT_PREFIXES:
        _segment_pending(prefix, pending[prefix], splitter)
    return rows


def _prepare_row(
    original: dict[str, object], pending: dict[str, list[_Pending]]
) -> dict[str, object]:
    """Copy one row, record each gate decision, and queue what needs the model."""
    row = dict(original)
    for prefix in TEXT_PREFIXES:
        text = row.get(f"{prefix}_text")
        status = sentence_gate(
            prefix, row.get(f"{prefix}_text_status"), text, row.get(f"{prefix}_language")
        )
        if status is None:
            pending[prefix].append((row, cast(str, text)))
        _set_sentences(row, prefix, None, status if status is not None else SENTENCE_SUCCESS)
    return row


def _segment_pending(prefix: str, pending: list[_Pending], splitter: SentenceSplitter) -> None:
    """Run one batched model call and scatter its results back onto the rows."""
    if not pending:
        return
    results = splitter.split([text for _, text in pending])
    # strict= is the check rather than a separate length comparison, so the two
    # can never drift apart silently.
    try:
        paired = list(zip(pending, results, strict=True))
    except ValueError as error:
        raise ValueError("sentence result count does not match input count") from error
    for (row, _), result in paired:
        _set_sentences(row, prefix, _normalized_sentences(result), SENTENCE_SUCCESS)


def _normalized_sentences(result: Sequence[str]) -> list[str]:
    """Strip model output and reject a non-empty text that yielded nothing."""
    sentences = [stripped for sentence in result if (stripped := sentence.strip())]
    if not sentences:
        raise ValueError("model returned no sentences for non-empty text")
    return sentences


def _set_sentences(
    row: dict[str, object], prefix: str, sentences: list[str] | None, status: str
) -> None:
    """Write the sentence triple for one website field."""
    row[f"{prefix}_sentences"] = sentences
    row[f"{prefix}_sentence_count"] = None if sentences is None else len(sentences)
    row[f"{prefix}_sentence_status"] = status


__all__ = ["TEXT_PREFIXES", "SentenceSplitter", "segment_batch", "sentence_gate"]
