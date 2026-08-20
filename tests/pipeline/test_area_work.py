"""Contracts for bounded, deterministic area-worker coordination."""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from osm_polygon_website_tag.pipeline.area_work import (
    DEFAULT_AREA_WORKERS,
    DEFAULT_MAX_IN_FLIGHT_AREAS,
    MAX_AREA_WORKERS,
    MAX_IN_FLIGHT_AREAS,
    AreaPayload,
    AreaResult,
    AreaWorkCoordinator,
    validate_area_settings,
)
from osm_polygon_website_tag.pipeline.area_work import (
    __all__ as area_work_exports,
)


def _payload(sequence: int) -> AreaPayload:
    return AreaPayload(
        sequence=sequence,
        source_pbf="synthetic-latest.osm.pbf",
        region="synthetic",
        tags_dict={"website": "https://example.org"},
        osm_type="way",
        osm_id=sequence,
        osm_version=1,
        osm_timestamp=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        candidate_kind="closed_way",
        raw_geojson='{"type":"Polygon","coordinates":[]}',
    )


def test_area_work_module_exposes_focused_boundary() -> None:
    assert set(area_work_exports) == {
        "DEFAULT_AREA_WORKERS",
        "DEFAULT_MAX_IN_FLIGHT_AREAS",
        "MAX_AREA_WORKERS",
        "MAX_IN_FLIGHT_AREAS",
        "AreaPayload",
        "AreaResult",
        "AreaWorkCoordinator",
        "validate_area_settings",
    }


def test_area_work_coordinator_bounds_and_preserves_order() -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def process(payload: AreaPayload) -> AreaResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03 if payload.sequence == 0 else 0.0)
        with lock:
            active -= 1
        return AreaResult(public_row={"sequence": payload.sequence})

    coordinator = AreaWorkCoordinator(
        area_workers=2,
        max_in_flight_areas=2,
        processor=process,
    )
    ready = []
    for sequence in range(4):
        result = coordinator.submit(_payload(sequence))
        if result is not None:
            ready.append(result)
    ready.extend(coordinator.close())

    assert peak <= 2
    assert [result.public_row["sequence"] for result in ready if result.public_row] == [
        0,
        1,
        2,
        3,
    ]


def test_area_worker_counts_produce_identical_records() -> None:
    payloads = [_payload(sequence) for sequence in range(4)]

    def process(payload: AreaPayload) -> AreaResult:
        return AreaResult(public_row={"sequence": payload.sequence})

    outputs = []
    for workers in (1, 3):
        coordinator = AreaWorkCoordinator(
            area_workers=workers,
            max_in_flight_areas=workers * 2,
            processor=process,
        )
        results = [coordinator.submit(payload) for payload in payloads]
        completed = [result for result in results if result is not None]
        completed.extend(coordinator.close())
        outputs.append([result.public_row for result in completed])

    assert outputs[0] == outputs[1]


def test_area_worker_settings_reject_unsafe_bounds() -> None:
    def process(payload: AreaPayload) -> AreaResult:
        return AreaResult(public_row={"sequence": payload.sequence})

    with pytest.raises(ValueError, match="area_workers"):
        AreaWorkCoordinator(
            area_workers=MAX_AREA_WORKERS + 1,
            max_in_flight_areas=DEFAULT_MAX_IN_FLIGHT_AREAS,
            processor=process,
        )
    with pytest.raises(ValueError, match="max_in_flight_areas"):
        AreaWorkCoordinator(
            area_workers=DEFAULT_AREA_WORKERS,
            max_in_flight_areas=MAX_IN_FLIGHT_AREAS + 1,
            processor=process,
        )


def test_validate_area_settings_and_abort_are_idempotent() -> None:
    validate_area_settings(1, 1)
    with pytest.raises(ValueError, match="area_workers"):
        validate_area_settings(0, 1)
    with pytest.raises(ValueError, match="max_in_flight_areas"):
        validate_area_settings(1, 0)
    coordinator = AreaWorkCoordinator(
        area_workers=1,
        max_in_flight_areas=1,
        processor=lambda payload: AreaResult(public_row={"sequence": payload.sequence}),
    )
    coordinator.submit(_payload(1))
    coordinator.abort()
    coordinator.abort()
    with pytest.raises(RuntimeError, match="closed"):
        coordinator.submit(_payload(2))
