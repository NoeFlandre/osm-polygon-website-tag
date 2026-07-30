"""Small deterministic Trafilatura adapter for downloaded HTML."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
from typing import Literal

import trafilatura

from .text_schema import count_words


@dataclass(frozen=True)
class TextExtraction:
    """Structured main-text extraction result."""

    status: Literal["success", "empty", "extract_error"]
    text: str | None
    word_count: int | None
    message: str | None
    trafilatura_version: str


def extract_main_text(html: bytes, *, url: str) -> TextExtraction:
    """Extract full main text from already downloaded HTML."""
    library_version = version("trafilatura")
    decoded = html.decode("utf-8", errors="replace")
    try:
        value = trafilatura.extract(
            decoded,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
        )
    except Exception as exc:
        return TextExtraction(
            "extract_error",
            None,
            None,
            type(exc).__name__,
            library_version,
        )
    if value is None or not value.strip():
        return TextExtraction("empty", "", 0, None, library_version)
    return TextExtraction("success", value, count_words(value), None, library_version)


__all__ = ["TextExtraction", "extract_main_text"]
