"""Tests for the UTC clock helpers in ``contracts.timestamps``."""

from __future__ import annotations

import datetime as dt

import pytest

import osm_polygon_website_tag.contracts.timestamps as clock
from osm_polygon_website_tag.contracts.timestamps import (
    utc_iso,
    utc_iso_seconds,
    utc_now,
    utc_run_id,
)


def test_utc_now_is_timezone_aware_utc() -> None:
    now = utc_now()

    assert now.tzinfo is dt.UTC
    assert now.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize(
    ("moment", "iso", "iso_seconds", "run_id"),
    [
        (
            dt.datetime(2026, 3, 14, 15, 9, 26, 535897, tzinfo=dt.UTC),
            "2026-03-14T15:09:26.535897+00:00",
            "2026-03-14T15:09:26+00:00",
            "20260314T150926Z",
        ),
        (
            dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
            "2026-01-02T03:04:05+00:00",
            "2026-01-02T03:04:05+00:00",
            "20260102T030405Z",
        ),
        (
            dt.datetime(2026, 12, 31, 23, 59, 59, 500000, tzinfo=dt.UTC),
            "2026-12-31T23:59:59.500000+00:00",
            "2026-12-31T23:59:59+00:00",
            "20261231T235959Z",
        ),
        (
            dt.datetime(2026, 10, 8, 0, 0, 0, 1, tzinfo=dt.UTC),
            "2026-10-08T00:00:00.000001+00:00",
            "2026-10-08T00:00:00+00:00",
            "20261008T000000Z",
        ),
    ],
)
def test_helpers_render_the_current_time_in_their_stored_formats(
    moment: dt.datetime, iso: str, iso_seconds: str, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "utc_now", lambda: moment)

    assert utc_iso() == iso
    assert utc_iso_seconds() == iso_seconds
    assert utc_run_id() == run_id


def test_utc_iso_seconds_truncates_the_given_moment_without_reading_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_clock() -> dt.datetime:
        raise AssertionError("an explicit moment must not read the clock")

    monkeypatch.setattr(clock, "utc_now", unexpected_clock)
    moment = dt.datetime(2024, 5, 6, 7, 8, 9, 987654, tzinfo=dt.UTC)

    assert utc_iso_seconds(moment) == "2024-05-06T07:08:09+00:00"
