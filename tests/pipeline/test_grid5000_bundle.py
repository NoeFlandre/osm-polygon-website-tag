"""Tests for the primitives both staged Grid'5000 stages share."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.pipeline import grid5000_bundle


def test_required_bool_names_the_field_it_rejects() -> None:
    assert grid5000_bundle.required_bool({"completed": True}, "completed") is True
    assert grid5000_bundle.required_bool({"completed": False}, "completed") is False

    with pytest.raises(ValueError, match=r"^completed must be a boolean$"):
        grid5000_bundle.required_bool({"completed": "true"}, "completed")
    with pytest.raises(ValueError, match=r"^changed must be a boolean$"):
        grid5000_bundle.required_bool({}, "changed")
