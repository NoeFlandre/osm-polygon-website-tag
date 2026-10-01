"""Synthetic polygon shard fixtures for pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fixtures.extraction import SIMPLE_OSM_XML, PBFBuilder, pbf_path


@pytest.fixture
def synthetic_source_simple(make_pbf: PBFBuilder) -> Path:
    """Build the shared synthetic source with one qualifying website polygon."""
    return pbf_path(make_pbf(SIMPLE_OSM_XML))
