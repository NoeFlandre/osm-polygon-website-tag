"""Fixed-clock pins for every UTC call site that writes or compares a timestamp.

The expected values were recorded by running the parent commit's call sites
(a0956e5) under the frozen clock below. Each test freezes the clock immediately
before the one call under test and asserts the stored or returned value exactly,
so the serialized forms (second versus microsecond precision, ``+00:00``, the
``Z`` run-ID stamp) cannot drift when the call sites change.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest

import osm_polygon_website_tag.pipeline.extraction as extraction_module
import osm_polygon_website_tag.web.politeness as politeness_module
import osm_polygon_website_tag.web.text_cache as text_cache_module
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_EXTRACTING,
    default_run_id,
    initialise_run,
    transition_status,
)
from osm_polygon_website_tag.web.politeness import retry_after_seconds
from osm_polygon_website_tag.web.text_cache import CachedText, TextCache

_REAL_DATETIME = dt.datetime
_UTC = dt.UTC

_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
<node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
<way id="100" version="2" timestamp="2024-01-01T00:00:00Z">
  <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
  <tag k="building" v="yes"/>
  <tag k="website" v="https://example.com"/>
</way>
</osm>
"""

# Four instants: a typical value, a whole second (isoformat drops the fraction),
# a trailing-zero microsecond, and a one-microsecond value (zero padding).
_INSTANTS: dict[str, dict[str, Any]] = {
    "micro": {
        "moment": _REAL_DATETIME(2026, 3, 14, 15, 9, 26, 535897, tzinfo=_UTC),
        "http_date": "Sat, 14 Mar 2026 15:10:00 GMT",
        "run_id": "20260314T150926Z",
        "created_at": "2026-03-14T15:09:26.535897+00:00",
        "status_changed_at": "2026-03-14T15:09:26.535897+00:00",
        "now_iso": "2026-03-14T15:09:26+00:00",
        "extract_started_at": "2026-03-14T15:09:26+00:00",
        "extract_finished_at": "2026-03-14T15:09:29+00:00",
        "extract_duration_seconds": 2.75,
        "last_attempt_at": "2026-03-14T15:09:26.535897+00:00",
        "quarantine_stamp": "20260314T150926535897Z",
        "retry_after_seconds": 33.464103,
    },
    "whole_second": {
        "moment": _REAL_DATETIME(2026, 1, 2, 3, 4, 5, tzinfo=_UTC),
        "http_date": "Fri, 02 Jan 2026 03:05:00 GMT",
        "run_id": "20260102T030405Z",
        "created_at": "2026-01-02T03:04:05+00:00",
        "status_changed_at": "2026-01-02T03:04:05+00:00",
        "now_iso": "2026-01-02T03:04:05+00:00",
        "extract_started_at": "2026-01-02T03:04:05+00:00",
        "extract_finished_at": "2026-01-02T03:04:07+00:00",
        "extract_duration_seconds": 2.75,
        "last_attempt_at": "2026-01-02T03:04:05+00:00",
        "quarantine_stamp": "20260102T030405000000Z",
        "retry_after_seconds": 55.0,
    },
    "trailing_zero": {
        "moment": _REAL_DATETIME(2026, 12, 31, 23, 59, 59, 500000, tzinfo=_UTC),
        "http_date": "Fri, 01 Jan 2027 00:00:00 GMT",
        "run_id": "20261231T235959Z",
        "created_at": "2026-12-31T23:59:59.500000+00:00",
        "status_changed_at": "2026-12-31T23:59:59.500000+00:00",
        "now_iso": "2026-12-31T23:59:59+00:00",
        "extract_started_at": "2026-12-31T23:59:59+00:00",
        "extract_finished_at": "2027-01-01T00:00:02+00:00",
        "extract_duration_seconds": 2.75,
        "last_attempt_at": "2026-12-31T23:59:59.500000+00:00",
        "quarantine_stamp": "20261231T235959500000Z",
        "retry_after_seconds": 0.5,
    },
    "one_microsecond": {
        "moment": _REAL_DATETIME(2026, 10, 8, 0, 0, 0, 1, tzinfo=_UTC),
        "http_date": "Thu, 08 Oct 2026 00:00:30 GMT",
        "run_id": "20261008T000000Z",
        "created_at": "2026-10-08T00:00:00.000001+00:00",
        "status_changed_at": "2026-10-08T00:00:00.000001+00:00",
        "now_iso": "2026-10-08T00:00:00+00:00",
        "extract_started_at": "2026-10-08T00:00:00+00:00",
        "extract_finished_at": "2026-10-08T00:00:02+00:00",
        "extract_duration_seconds": 2.75,
        "last_attempt_at": "2026-10-08T00:00:00.000001+00:00",
        "quarantine_stamp": "20261008T000000000001Z",
        "retry_after_seconds": 29.999999,
    },
}
_IDS = sorted(_INSTANTS)


def _freeze(monkeypatch: pytest.MonkeyPatch, *moments: _REAL_DATETIME) -> None:
    """Make successive ``now()`` calls return ``moments``, repeating the last one.

    Call sites read the clock as ``dt.datetime.now(...)`` through ``import datetime
    as dt``, so the class is replaced on the stdlib module. ``politeness`` binds the
    class by name at import, so its module attribute is replaced as well.
    """
    queue = list(moments)

    class FrozenDatetime(_REAL_DATETIME):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> _REAL_DATETIME:
            moment = queue.pop(0) if len(queue) > 1 else queue[0]
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(dt, "datetime", FrozenDatetime)
    monkeypatch.setattr(politeness_module, "datetime", FrozenDatetime)


def _pbf(make_pbf: Any) -> Path:
    directory = make_pbf(_XML, name="testland-latest.osm.pbf")
    return next(p for p in directory.iterdir() if p.name.endswith(".osm.pbf"))


def _cached_text() -> CachedText:
    return CachedText(
        url="https://example.org",
        status="success",
        text="full text",
        word_count=2,
        final_url="https://example.org/",
        message=None,
        attempt_count=0,
        last_attempt_at="",
        trafilatura_version="2.1.0",
        invocation_id="",
    )


@pytest.mark.parametrize("instant", _IDS)
def test_run_id_matches_parent_output(instant: str, monkeypatch: pytest.MonkeyPatch) -> None:
    golden = _INSTANTS[instant]
    _freeze(monkeypatch, golden["moment"])

    assert default_run_id() == golden["run_id"]


@pytest.mark.parametrize("instant", _IDS)
def test_run_json_created_at_matches_parent_output(
    instant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _INSTANTS[instant]
    _freeze(monkeypatch, golden["moment"])

    run_dir, _ = initialise_run(tmp_path, run_id="r1")

    stored = json.loads((run_dir / "manifests" / "run.json").read_text(encoding="utf-8"))
    assert stored["created_at"] == golden["created_at"]


@pytest.mark.parametrize("instant", _IDS)
def test_run_json_status_changed_at_matches_parent_output(
    instant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _INSTANTS[instant]
    run_dir, state = initialise_run(tmp_path, run_id="r1")
    _freeze(monkeypatch, golden["moment"])

    transition_status(state, STATUS_EXTRACTING)

    stored = json.loads((run_dir / "manifests" / "run.json").read_text(encoding="utf-8"))
    assert stored["status_changed_at"] == golden["status_changed_at"]


@pytest.mark.parametrize("instant", _IDS)
def test_extraction_now_iso_matches_parent_output(
    instant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _INSTANTS[instant]
    _freeze(monkeypatch, golden["moment"])

    assert extraction_module._now_iso() == golden["now_iso"]


@pytest.mark.parametrize("instant", _IDS)
def test_extract_pbf_timestamps_match_parent_output(
    instant: str, make_pbf: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _INSTANTS[instant]
    src = _pbf(make_pbf)
    run_dir, state = initialise_run(tmp_path, run_id="r3")
    moment = golden["moment"]
    _freeze(monkeypatch, moment, moment + dt.timedelta(microseconds=2_750_000))

    result = extract_pbf(src, run_dir, state)

    assert result.started_at == golden["extract_started_at"]
    assert result.finished_at == golden["extract_finished_at"]
    assert result.duration_seconds == golden["extract_duration_seconds"]
    entries = json.loads((run_dir / "manifests" / "sources.json").read_text(encoding="utf-8"))
    assert entries[0]["started_at"] == golden["extract_started_at"]
    assert entries[0]["finished_at"] == golden["extract_finished_at"]


@pytest.mark.parametrize("instant", _IDS)
def test_text_cache_last_attempt_at_matches_parent_output(
    instant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _INSTANTS[instant]
    cache = TextCache(tmp_path / "text.sqlite3")
    _freeze(monkeypatch, golden["moment"])

    try:
        stored = cache.record(_cached_text(), invocation_id="run-1")
    finally:
        cache.close()

    assert stored.last_attempt_at == golden["last_attempt_at"]


@pytest.mark.parametrize("instant", _IDS)
def test_text_cache_quarantine_stamp_matches_parent_output(
    instant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _INSTANTS[instant]
    path = tmp_path / "text.sqlite3"
    path.write_bytes(b"not a database")
    _freeze(monkeypatch, golden["moment"])

    quarantine = text_cache_module._quarantine_corrupt_database(path)

    prefix = f"text.sqlite3.corrupt-{golden['quarantine_stamp']}-"
    assert quarantine.name.startswith(prefix)
    assert re.fullmatch(r"[0-9a-f]{32}", quarantine.name.removeprefix(prefix))


@pytest.mark.parametrize("instant", _IDS)
def test_retry_after_default_now_matches_parent_output(
    instant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _INSTANTS[instant]
    _freeze(monkeypatch, golden["moment"])

    assert retry_after_seconds(golden["http_date"]) == golden["retry_after_seconds"]
