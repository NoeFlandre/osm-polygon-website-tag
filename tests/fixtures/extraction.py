"""Shared synthetic polygon inputs for extraction tests."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol

from osm_polygon_website_tag.pipeline.area_work import AreaPayload
from osm_polygon_website_tag.pipeline.record_builders import DerivedTags

SIMPLE_OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
<node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
<way id="100" version="2" timestamp="2024-01-01T00:00:00Z">
  <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
  <tag k="building" v="yes"/>
  <tag k="name" v="Building A"/>
  <tag k="website" v="https://example.com"/>
  <tag k="wikidata" v="Q42"/>
</way>
<way id="101" version="1" timestamp="2024-01-01T00:00:00Z">
  <nd ref="1"/><nd ref="2"/><nd ref="3"/>
  <tag k="highway" v="residential"/>
</way>
<way id="102" version="1" timestamp="2024-01-01T00:00:00Z">
  <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
  <tag k="building" v="yes"/>
</way>
</osm>
"""


class PBFBuilder(Protocol):
    """Signature of the ``make_pbf`` fixture in ``tests/conftest.py``."""

    def __call__(self, xml: str, *, name: str = "monaco-latest.osm.pbf") -> Path: ...


def pbf_path(src_dir: Path, name: str = "monaco-latest.osm.pbf") -> Path:
    """Return the actual PBF inside ``src_dir`` (named by the ``make_pbf`` call)."""
    for entry in src_dir.iterdir():
        if entry.name.endswith(".osm.pbf"):
            return entry
    return src_dir / name


def website_payload(raw_geojson: str) -> AreaPayload:
    """Build the canonical website polygon payload used by extraction tests."""
    return AreaPayload(
        sequence=1,
        source_pbf="synthetic-latest.osm.pbf",
        region="synthetic",
        tags_dict={"website": "https://example.org", "building": "yes"},
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        candidate_kind="closed_way",
        raw_geojson=raw_geojson,
        derived_tags=DerivedTags(
            website="https://example.org",
            contact_website=None,
            has_website=True,
            has_contact_website=False,
            has_any_website=True,
            primary_category="building",
        ),
    )
