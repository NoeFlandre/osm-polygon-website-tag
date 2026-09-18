"""Contract tests for geographic reporting value objects."""

from __future__ import annotations

from typing import cast

import pytest

from osm_polygon_website_tag.reporting.geographic.models import (
    AggregationMode,
    PolygonDensitySummary,
)


def test_summary_defaults_to_regional_rows() -> None:
    summary = PolygonDensitySummary(3, 0, 0, ())

    assert summary.aggregation_mode == "regional_rows"
    assert summary.extracted_text_only is False


def test_summary_legacy_text_flag_maps_to_global_unique_text() -> None:
    summary = PolygonDensitySummary(3, 0, 0, (), extracted_text_only=True)

    assert summary.aggregation_mode == "global_unique_text"
    assert summary.extracted_text_only is True


def test_summary_explicit_global_mode_sets_legacy_text_flag() -> None:
    summary = PolygonDensitySummary(
        3,
        0,
        0,
        (),
        aggregation_mode="global_unique_text",
    )

    assert summary.aggregation_mode == "global_unique_text"
    assert summary.extracted_text_only is True


def test_summary_explicit_regional_mode_clears_legacy_text_flag() -> None:
    summary = PolygonDensitySummary(
        3,
        0,
        0,
        (),
        extracted_text_only=True,
        aggregation_mode="regional_rows",
    )

    assert summary.aggregation_mode == "regional_rows"
    assert summary.extracted_text_only is False


def test_summary_rejects_unknown_aggregation_mode() -> None:
    invalid_mode = cast(AggregationMode, "unsupported")

    with pytest.raises(ValueError, match="unsupported aggregation mode"):
        PolygonDensitySummary(3, 0, 0, (), aggregation_mode=invalid_mode)
