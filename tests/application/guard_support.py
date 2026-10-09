"""Detection rule for the producer guard in conftest.py, kept importable for its own test."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import pytest

from osm_polygon_website_tag.application.progress import CountedProgress

# Any string with the counted prefix, including empty and multi-line text.
_COUNTED_PREFIX = re.compile(r"\[\d+/\d+\] ")


def plain_counted_texts(messages: Iterable[object]) -> list[str]:
    """Return the messages that look counted but are not CountedProgress."""
    return [
        str(message)
        for message in messages
        if not isinstance(message, CountedProgress) and _COUNTED_PREFIX.match(str(message))
    ]


def install_producer_guard(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    """Route ``module.report_progress`` through the detection rule and return what it finds.

    The returned list grows as producers send messages. The conftest fixture asserts
    it is empty when a test ends.
    """
    forward = module.report_progress
    plain_counted: list[str] = []

    def record(callback: Any, message: object) -> None:
        plain_counted.extend(plain_counted_texts([message]))
        forward(callback, message)

    monkeypatch.setattr(module, "report_progress", record)
    return plain_counted
