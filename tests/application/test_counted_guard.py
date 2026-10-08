"""The producer guard's detection rule, tested directly."""

from __future__ import annotations

import types

import pytest
from tests.application.guard_support import install_producer_guard, plain_counted_texts

from osm_polygon_website_tag.application.progress import counted_progress


@pytest.mark.parametrize(
    "message",
    ["[1/2] Extracting a.osm.pbf", "[3/4] ", "[1/2] first\nsecond"],
)
def test_the_guard_flags_a_plain_counted_string_including_empty_and_multiline(
    message: str,
) -> None:
    assert plain_counted_texts([message]) == [message]


@pytest.mark.parametrize(
    "message",
    [
        counted_progress(1, 2, "Extracting a.osm.pbf"),
        counted_progress(3, 4, ""),
        "kept",
        "ignored",
        "Extracting a.osm.pbf",
    ],
)
def test_the_guard_passes_typed_and_ordinary_messages(message: object) -> None:
    assert plain_counted_texts([message]) == []


def test_the_guard_wiring_records_plain_counted_text_and_still_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: list[object] = []
    module = types.SimpleNamespace(
        report_progress=lambda _callback, message: forwarded.append(message)
    )
    plain = install_producer_guard(monkeypatch, module)

    module.report_progress(None, "[1/2] Extracting a.osm.pbf")
    module.report_progress(None, counted_progress(1, 2, "ok"))

    assert plain == ["[1/2] Extracting a.osm.pbf"]
    assert forwarded == ["[1/2] Extracting a.osm.pbf", "[1/2] ok"]
