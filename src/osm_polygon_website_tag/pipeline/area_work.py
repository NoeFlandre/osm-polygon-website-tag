"""Bounded FIFO coordination for pure area-payload processing."""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from osm_polygon_website_tag.pipeline.record_builders import DerivedTags

DEFAULT_AREA_WORKERS = 4
MAX_AREA_WORKERS = 16
DEFAULT_MAX_IN_FLIGHT_AREAS = 32
MAX_IN_FLIGHT_AREAS = 256


@dataclass(frozen=True)
class AreaPayload:
    """Copied data safe to pass from a libosmium callback to a worker."""

    sequence: int
    source_pbf: str
    region: str
    tags_dict: dict[str, str]
    osm_type: str
    osm_id: int
    osm_version: int
    osm_timestamp: dt.datetime
    candidate_kind: str
    raw_geojson: str | None
    derived_tags: DerivedTags | None = None


@dataclass(frozen=True)
class AreaResult:
    """Rows produced by one area worker, in deterministic input order."""

    public_row: dict[str, object] | None = None
    observation_row: dict[str, object] | None = None
    rejection_row: dict[str, object] | None = None


def validate_area_settings(area_workers: int, max_in_flight_areas: int) -> None:
    """Reject worker settings outside the bounded extraction contract."""
    if not 1 <= area_workers <= MAX_AREA_WORKERS:
        raise ValueError(f"area_workers must be between 1 and {MAX_AREA_WORKERS}")
    if not 1 <= max_in_flight_areas <= MAX_IN_FLIGHT_AREAS:
        raise ValueError(f"max_in_flight_areas must be between 1 and {MAX_IN_FLIGHT_AREAS}")


class AreaWorkCoordinator:
    """Bounded FIFO executor for pure area payload processing."""

    def __init__(
        self,
        *,
        area_workers: int,
        max_in_flight_areas: int,
        processor: Callable[[AreaPayload], AreaResult],
    ) -> None:
        validate_area_settings(area_workers, max_in_flight_areas)
        self._executor = ThreadPoolExecutor(
            max_workers=area_workers,
            thread_name_prefix="area-worker",
        )
        self._max_in_flight = max_in_flight_areas
        self._processor = processor
        self._pending: deque[Future[AreaResult]] = deque()
        self._closed = False

    def submit(self, payload: AreaPayload) -> AreaResult | None:
        if self._closed:
            raise RuntimeError("area work coordinator is closed")
        self._pending.append(self._executor.submit(self._processor, payload))
        if len(self._pending) >= self._max_in_flight:
            return self._pending.popleft().result()
        return None

    def drain(self) -> list[AreaResult]:
        results: list[AreaResult] = []
        while self._pending:
            results.append(self._pending.popleft().result())
        return results

    def close(self) -> list[AreaResult]:
        if self._closed:
            return []
        try:
            return self.drain()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._closed = True

    def abort(self) -> None:
        if self._closed:
            return
        self._pending.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._closed = True


__all__ = [
    "DEFAULT_AREA_WORKERS",
    "DEFAULT_MAX_IN_FLIGHT_AREAS",
    "MAX_AREA_WORKERS",
    "MAX_IN_FLIGHT_AREAS",
    "AreaPayload",
    "AreaResult",
    "AreaWorkCoordinator",
    "validate_area_settings",
]
