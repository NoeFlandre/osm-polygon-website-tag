"""Shared deadline and batch-budget rules for bounded pipeline stages."""

from __future__ import annotations

from collections.abc import Callable

from osm_polygon_website_tag.pipeline.grid5000_bundle import validate_positive_grid_time


def start_deadline(
    time_budget_seconds: float | None,
    clock: Callable[[], float],
) -> float | None:
    """Return a monotonic deadline for a bounded stage invocation."""
    if time_budget_seconds is None:
        return None
    return clock() + time_budget_seconds


def deadline_reached(deadline: float | None, clock: Callable[[], float]) -> bool:
    """Return whether the stage has reached its monotonic deadline."""
    return deadline is not None and clock() >= deadline


def seconds_remaining(deadline: float | None, clock: Callable[[], float]) -> float | None:
    """Return the time left before a deadline, or ``None`` when unbounded."""
    if deadline is None:
        return None
    return deadline - clock()


def budget_exhausted(remaining_seconds: float | None) -> bool:
    """Return whether a previously measured remaining budget is exhausted."""
    return remaining_seconds is not None and remaining_seconds <= 0


def validate_batch_options(batch_rows: int, time_budget_seconds: float | None) -> None:
    """Validate the shared positive batch size and optional time budget."""
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")
    if time_budget_seconds is not None:
        validate_positive_grid_time(time_budget_seconds)


__all__ = [
    "budget_exhausted",
    "deadline_reached",
    "seconds_remaining",
    "start_deadline",
    "validate_batch_options",
]
