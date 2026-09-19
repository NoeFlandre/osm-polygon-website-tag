"""Tests for the per-PBF polygon extraction."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.pipeline.area_work import (
    AreaPayload,
)
from osm_polygon_website_tag.pipeline.extraction import (
    extract_pbf,
)
from osm_polygon_website_tag.pipeline.record_builders import DerivedTags

_SIMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
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


def _pbf_path(src_dir: Path, name: str = "monaco-latest.osm.pbf") -> Path:
    """Return the actual PBF inside ``src_dir`` (named by the
    ``make_pbf`` call)."""
    for entry in src_dir.iterdir():
        if entry.name.endswith(".osm.pbf"):
            return entry
    return src_dir / name


@pytest.fixture()
def synthetic_source_simple(make_pbf) -> Path:
    return _pbf_path(make_pbf(_SIMPLE_XML))


def _website_payload(raw_geojson: str) -> AreaPayload:
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


def test_extract_emits_comparison_observation_for_qualifying_object(
    synthetic_source_simple: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    obs = pq.read_table(run_dir / "analysis_observations" / "monaco-latest.parquet").to_pylist()
    assert len(obs) == 1
    row = obs[0]
    assert row["has_website"] is True
    assert row["has_contact_website"] is False
    assert row["has_any_website"] is True
    assert row["has_wikidata"] is True
    assert row["wikidata"] == "Q42"


def test_extract_wikidata_only_object_enters_comparison_only(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="wikidata" v="Q42"/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = extract_pbf(src, run_dir)
    assert result.public_row_count == 0
    assert result.observation_row_count == 1
    # Public shard empty, comparison shard has the row, rejections empty.
    pub = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert pub.num_rows == 0
    obs = pq.read_table(run_dir / "analysis_observations" / "monaco-latest.parquet").to_pylist()
    assert len(obs) == 1
    assert obs[0]["has_wikidata"] is True
    assert obs[0]["has_any_website"] is False
    rej = pq.read_table(run_dir / "rejections" / "monaco-latest.parquet")
    assert rej.num_rows == 0


def test_extract_malformed_wikidata_retained_with_null_qid(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://example.com"/>
    <tag k="wikidata" v="http://www.wikidata.org/wiki/Q42"/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "analysis_observations" / "monaco-latest.parquet")
    assert table["wikidata"][0].as_py() == "http://www.wikidata.org/wiki/Q42"
