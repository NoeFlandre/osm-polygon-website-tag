"""Direct contracts for bounded geographic input helpers."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from osm_polygon_website_tag.contracts.arrow import call_arrow_kernel
from osm_polygon_website_tag.reporting.geographic.inputs import (
    _coordinate_value,
    _path_columns,
    _row_is_eligible,
    _text_success_mask,
    _validated_coordinates,
)


def test_input_helpers_select_columns_and_validate_coordinates() -> None:
    names = {"lat", "lon", "website_text_status", "contact_website_text_status"}
    assert _path_columns(Path("a.parquet"), names, extracted_text_only=False) == ["lat", "lon"]
    assert _path_columns(Path("a.parquet"), names, extracted_text_only=True) == [
        "lat",
        "lon",
        "website_text_status",
        "contact_website_text_status",
    ]
    assert _row_is_eligible(None, 0)
    assert _row_is_eligible([False, True], 1)
    assert not _row_is_eligible([False, True], 0)
    assert _coordinate_value(Path("a.parquet"), 2, 1, 2, lat_is_null=False, lon_is_null=False) == (
        Path("a.parquet"),
        2,
        1.0,
        2.0,
    )
    with pytest.raises(ValueError, match="null coordinate"):
        _coordinate_value(Path("a.parquet"), 2, None, 2, lat_is_null=True, lon_is_null=False)


def test_input_helpers_build_null_safe_text_mask_and_coordinate_iterator() -> None:
    batch = pa.record_batch(
        [
            pa.array(["success", None, "absent"]),
            pa.array([None, "success", "absent"]),
        ],
        names=["website_text_status", "contact_website_text_status"],
    )
    assert _text_success_mask(batch).tolist() == [True, True, False]
    values = zip(
        [1.0, 2.0, 3.0],
        [4.0, 5.0, 6.0],
        [False, False, False],
        [False, False, False],
        strict=True,
    )
    assert list(_validated_coordinates(Path("a.parquet"), values, [True, False, True], 10)) == [
        (Path("a.parquet"), 10, 1.0, 4.0),
        (Path("a.parquet"), 12, 3.0, 6.0),
    ]
    assert call_arrow_kernel("equal", pa.array(["success"]), "success").to_pylist() == [True]
