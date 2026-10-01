"""Tests for the shared bounded-stage budget rules."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.pipeline.time_budget import (
    budget_exhausted,
    deadline_reached,
    seconds_remaining,
    start_deadline,
    validate_batch_options,
)


def test_start_deadline_does_not_read_the_clock_for_unbounded_stages() -> None:
    def unexpected_clock() -> float:
        raise AssertionError("an unbounded stage does not need a deadline")

    assert start_deadline(None, unexpected_clock) is None


def test_deadline_uses_an_inclusive_boundary_and_preserves_remaining_time() -> None:
    def clock() -> float:
        return 2.0

    def initial_clock() -> float:
        return 1.0

    deadline = start_deadline(3.0, initial_clock)

    assert deadline == 4.0
    assert deadline_reached(deadline, clock) is False
    assert seconds_remaining(deadline, clock) == 2.0
    assert deadline_reached(deadline, lambda: 4.0) is True


@pytest.mark.parametrize("remaining", [None, 1.0, 0.0, -1.0])
def test_budget_exhaustion_matches_an_optional_remaining_duration(remaining: float | None) -> None:
    assert budget_exhausted(remaining) is (remaining is not None and remaining <= 0)


def test_batch_options_keep_the_shared_validation_contract() -> None:
    assert validate_batch_options(1, None) is None
    with pytest.raises(ValueError, match="batch_rows must be positive"):
        validate_batch_options(0, None)
    with pytest.raises(ValueError):
        validate_batch_options(1, 0.0)
