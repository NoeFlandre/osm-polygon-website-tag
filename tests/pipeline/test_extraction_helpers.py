"""Tests for the per-PBF polygon extraction."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import osmium
import osmium.osm
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.extraction as extraction_module
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.pipeline import extraction_handler as extraction_handler_module
from osm_polygon_website_tag.pipeline.area_work import (
    AreaPayload,
    AreaResult,
)
from osm_polygon_website_tag.pipeline.extraction import (
    ExtractionResult,
    _ExtractionHandler,
    extract_pbf,
)
from osm_polygon_website_tag.pipeline.record_builders import DerivedTags
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink

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


def test_extraction_private_helpers_validate_osm_metadata() -> None:
    assert extraction_module._as_utc(dt.datetime(2024, 1, 1, tzinfo=dt.UTC)).tzinfo is dt.UTC
    naive = extraction_module._as_utc(dt.datetime(2024, 1, 1))
    assert naive.tzinfo is dt.UTC
    assert extraction_module._now_iso().endswith("+00:00")
    way: Any = SimpleNamespace(
        nodes=[
            SimpleNamespace(ref=1),
            SimpleNamespace(ref=2),
            SimpleNamespace(ref=3),
            SimpleNamespace(ref=1),
        ]
    )
    open_way: Any = SimpleNamespace(
        nodes=[SimpleNamespace(ref=1), SimpleNamespace(ref=2), SimpleNamespace(ref=3)]
    )
    duplicate_way: Any = SimpleNamespace(
        nodes=[
            SimpleNamespace(ref=1),
            SimpleNamespace(ref=2),
            SimpleNamespace(ref=2),
            SimpleNamespace(ref=1),
        ]
    )
    assert extraction_handler_module._is_closed_way(way)
    assert not extraction_handler_module._is_closed_way(
        cast(osmium.osm.Way, SimpleNamespace(nodes=[]))
    )
    assert not extraction_handler_module._is_closed_way(open_way)
    assert not extraction_handler_module._is_closed_way(duplicate_way)
    relation: Any = SimpleNamespace(tags=[("type", "multipolygon")])
    boundary: Any = SimpleNamespace(tags=[("type", "boundary")])
    unsupported: Any = SimpleNamespace(tags=[("type", "route")])
    assert extraction_handler_module._is_supported_polygon_relation(relation)
    assert extraction_handler_module._is_supported_polygon_relation(boundary)
    assert not extraction_handler_module._is_supported_polygon_relation(unsupported)
    tagged: Any = SimpleNamespace(tags=[("website", "x"), ("name", "n")])
    assert extraction_handler_module._tags_dict(tagged) == {
        "website": "x",
        "name": "n",
    }
    way_area: Any = SimpleNamespace(from_way=lambda: True, orig_id=lambda: 7)
    relation_area: Any = SimpleNamespace(from_way=lambda: False, orig_id=lambda: 8)
    assert extraction_handler_module._area_identity(way_area) == ("way", 7)
    assert extraction_handler_module._area_identity(relation_area) == ("relation", 8)


def test_area_rejection_record_reuses_area_metadata() -> None:
    area: Any = SimpleNamespace(
        tags=[("website", "https://example.org")],
        version=3,
        timestamp=dt.datetime(2024, 1, 1),
    )

    row = extraction_handler_module._area_rejection_record(
        area,
        source_pbf="monaco-latest.osm.pbf",
        region="monaco",
        osm_type="way",
        osm_id=42,
        rejection_kind="geometry_error",
        message="broken geometry",
    )

    assert row["source_pbf"] == "monaco-latest.osm.pbf"
    assert row["osm_type"] == "way"
    assert row["osm_id"] == 42
    assert row["region"] == "monaco"
    assert row["osm_version"] == 3
    assert row["candidate_kind"] == "closed_way"
    assert row["rejection_kind"] == "geometry_error"
    assert row["message"] == "broken geometry"
    assert row["osm_timestamp"] == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)

    relation_row = extraction_handler_module._area_rejection_record(
        area,
        source_pbf="monaco-latest.osm.pbf",
        region="monaco",
        osm_type="relation",
        osm_id=43,
        rejection_kind="untracked_candidate",
        message="not tracked",
    )
    assert relation_row["candidate_kind"] == "relation_polygon"
    assert relation_row["osm_id"] == 43
    assert relation_row["message"] == "not tracked"


def test_extraction_handler_emits_and_records_area_results() -> None:
    emitted: list[dict[str, object]] = []
    coordinator_results = [
        AreaResult(public_row={"kind": "public"}, observation_row={"kind": "obs"})
    ]
    handler: Any = object.__new__(_ExtractionHandler)
    handler.public_sink = SimpleNamespace(add=lambda row: emitted.append({"public": row}))
    handler.obs_sink = SimpleNamespace(add=lambda row: emitted.append({"observation": row}))
    handler.rej_sink = SimpleNamespace(add=lambda row: emitted.append({"rejection": row}))
    handler._area_coordinator = SimpleNamespace(
        drain=lambda: coordinator_results,
        submit=lambda _payload: None,
    )
    handler._emit_area_result(
        AreaResult(public_row={"p": 1}, observation_row={"o": 2}, rejection_row={"r": 3})
    )
    assert emitted == [{"public": {"p": 1}}, {"observation": {"o": 2}}, {"rejection": {"r": 3}}]
    handler._drain_area_work()
    assert emitted[-1] == {"observation": {"kind": "obs"}}
    seen: list[tuple[object, ...]] = []
    handler.ledger = SimpleNamespace(upsert=lambda *args: seen.append(args))
    timestamp = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    tags = {"website": "x"}
    handler._record_candidate("way", 1, tags, 2, timestamp, "closed_way")
    assert seen == [("way", 1, tags, 2, timestamp, "closed_way")]


def test_extract_writes_three_shards_per_source(
    synthetic_source_simple: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = extract_pbf(synthetic_source_simple, run_dir)
    assert isinstance(result, ExtractionResult)
    assert result.public_row_count == 1
    # public shard
    assert (run_dir / "polygons" / "monaco-latest.parquet").exists()
    # comparison shard
    assert (run_dir / "analysis_observations" / "monaco-latest.parquet").exists()
    # rejection shard
    assert (run_dir / "rejections" / "monaco-latest.parquet").exists()
    # all three match their respective schemas
    pub = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert pub.schema.equals(POLYGON_PUBLIC_SCHEMA)
    row = pub.to_pylist()[0]
    assert row["website_text_status"] == "pending"
    assert row["contact_website_text_status"] == "absent"
    cmp_ = pq.read_table(run_dir / "analysis_observations" / "monaco-latest.parquet")
    assert cmp_.schema.equals(COMPARISON_OBSERVATION_SCHEMA)
    rej = pq.read_table(run_dir / "rejections" / "monaco-latest.parquet")
    assert rej.schema.equals(REJECTION_SCHEMA)


def test_handler_has_no_source_sized_python_collections(tmp_path: Path) -> None:
    handler = _ExtractionHandler(
        source_pbf="synthetic-latest.osm.pbf",
        region="synthetic",
        stem="synthetic-latest",
        polygons_dir=tmp_path / "polygons",
        obs_dir=tmp_path / "observations",
        rej_dir=tmp_path / "rejections",
    )

    assert not hasattr(handler, "_public_rows")
    assert not hasattr(handler, "_obs_rows")
    assert not hasattr(handler, "_rej_rows")
    assert not hasattr(handler, "_candidates")
    assert not hasattr(handler, "_area_seen")
    assert handler._area_sequence == 0


def test_geometry_rejection_is_written_with_candidate_metadata() -> None:
    rows: list[dict[str, object]] = []
    handler: Any = object.__new__(_ExtractionHandler)
    handler._source_pbf = "monaco-latest.osm.pbf"
    handler._region = "monaco"
    handler.rej_sink = cast(BatchParquetSink, SimpleNamespace(add=rows.append))
    area: Any = SimpleNamespace(
        tags=[("website", "https://example.org"), ("building", "yes")],
        from_way=lambda: True,
        orig_id=lambda: 42,
        version=3,
        timestamp=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
    )

    handler._flush_geometry_rejection(
        cast(osmium.osm.Area, area), "antimeridian", "crosses antimeridian"
    )

    assert rows[0]["rejection_kind"] == "antimeridian"
    assert rows[0]["osm_type"] == "way"
    assert rows[0]["osm_id"] == 42
    assert rows[0]["source_pbf"] == "monaco-latest.osm.pbf"
    assert rows[0]["region"] == "monaco"
    assert rows[0]["osm_version"] == 3
    assert rows[0]["candidate_kind"] == "closed_way"
    assert rows[0]["message"] == "crosses antimeridian"

    relation = SimpleNamespace(**{**vars(area), "from_way": lambda: False, "orig_id": lambda: 43})
    handler._flush_geometry_rejection(
        cast(osmium.osm.Area, relation), "invalid_geometry", "invalid relation geometry"
    )
    assert rows[1]["osm_type"] == "relation"
    assert rows[1]["candidate_kind"] == "relation_polygon"
    assert rows[1]["osm_id"] == 43
    assert rows[1]["message"] == "invalid relation geometry"


def test_extraction_handler_payload_and_callback_rejection_helpers() -> None:
    rows: list[dict[str, object]] = []
    handler: Any = object.__new__(_ExtractionHandler)
    handler._source_pbf = "synthetic-latest.osm.pbf"
    handler._region = "synthetic"
    handler._area_sequence = 7
    handler.rej_sink = cast(BatchParquetSink, SimpleNamespace(add=rows.append))
    serialized_areas: list[object] = []
    handler._serialize_area_geometry = lambda area: (
        serialized_areas.append(area) or '{"type":"Polygon"}'
    )
    handler._area_coordinator = SimpleNamespace(submit=lambda _payload: None)
    area: Any = SimpleNamespace(
        version=2,
        timestamp=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        tags=[("website", "https://example.org"), ("building", "yes")],
        from_way=lambda: True,
        orig_id=lambda: 1,
    )
    derived = _website_payload("unused").derived_tags
    candidate = {
        "tags": {"website": "https://example.org", "building": "yes"},
        "candidate_kind": "closed_way",
    }
    payload = handler._build_area_payload(cast(osmium.osm.Area, area), "way", 1, candidate, derived)
    assert payload is not None
    assert payload.sequence == 7
    assert serialized_areas == [area]
    assert payload.source_pbf == "synthetic-latest.osm.pbf"
    assert payload.region == "synthetic"
    assert payload.tags_dict == candidate["tags"]
    assert payload.osm_type == "way"
    assert payload.osm_id == 1
    assert payload.osm_version == 2
    assert payload.osm_timestamp == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert payload.candidate_kind == "closed_way"
    assert payload.raw_geojson == '{"type":"Polygon"}'
    assert payload.derived_tags is derived
    handler._serialize_area_geometry = lambda _area: None
    assert (
        handler._build_area_payload(cast(osmium.osm.Area, area), "way", 1, candidate, derived)
        is None
    )
    handler._emit_area_rejection(
        cast(osmium.osm.Area, area), "way", 1, "untracked_candidate", "not tracked"
    )
    assert rows[-1]["rejection_kind"] == "untracked_candidate"
    handler._submit_candidate_area(cast(osmium.osm.Area, area), "way", 1, candidate)


def test_submit_candidate_area_filters_and_orders_work() -> None:
    submitted: list[AreaPayload] = []
    emitted: list[AreaResult] = []
    handler: Any = object.__new__(_ExtractionHandler)
    handler._source_pbf = "synthetic-latest.osm.pbf"
    handler._region = "synthetic"
    handler._area_sequence = 7
    handler._serialize_area_geometry = lambda _area: '{"type":"Polygon"}'
    handler._area_coordinator = SimpleNamespace(
        submit=lambda payload: (
            submitted.append(payload) or AreaResult(public_row={"kind": "public"})
        )
    )
    handler._emit_area_result = emitted.append
    area: Any = SimpleNamespace(
        version=2,
        timestamp=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        tags=[("website", "https://example.org")],
    )

    handler._submit_candidate_area(
        cast(osmium.osm.Area, area),
        "way",
        1,
        {"tags": {"website": "https://example.org"}, "candidate_kind": "closed_way"},
    )
    handler._submit_candidate_area(
        cast(osmium.osm.Area, area),
        "relation",
        2,
        {"tags": {"wikidata": "Q42"}, "candidate_kind": "relation_polygon"},
    )
    handler._submit_candidate_area(
        cast(osmium.osm.Area, area),
        "way",
        3,
        {"tags": {"building": "yes"}, "candidate_kind": "closed_way"},
    )

    assert [payload.sequence for payload in submitted] == [7, 8]
    assert submitted[0].raw_geojson == '{"type":"Polygon"}'
    assert submitted[1].raw_geojson is None
    assert submitted[1].osm_type == "relation"
    assert submitted[1].osm_id == 2
    assert emitted == [
        AreaResult(public_row={"kind": "public"}),
        AreaResult(public_row={"kind": "public"}),
    ]
    assert handler._area_sequence == 9

    handler._serialize_area_geometry = lambda _area: None
    handler._submit_candidate_area(
        cast(osmium.osm.Area, area),
        "way",
        4,
        {"tags": {"website": "https://example.org"}, "candidate_kind": "closed_way"},
    )
    assert len(submitted) == 2
    assert handler._area_sequence == 9


def test_area_callback_reports_untracked_candidate_with_full_identity() -> None:
    rows: list[dict[str, object]] = []
    handler: Any = object.__new__(_ExtractionHandler)
    handler._source_pbf = "synthetic-latest.osm.pbf"
    handler._region = "synthetic"
    handler.rej_sink = cast(BatchParquetSink, SimpleNamespace(add=rows.append))
    handler.ledger = SimpleNamespace(mark_area_seen=lambda _osm_type, _osm_id: False)
    handler._drain_area_work = lambda: None
    area: Any = SimpleNamespace(
        version=4,
        timestamp=dt.datetime(2024, 1, 1),
        tags=[("website", "https://example.org"), ("building", "yes")],
        from_way=lambda: True,
        orig_id=lambda: 99,
    )

    handler.area(cast(osmium.osm.Area, area))

    assert rows[0]["source_pbf"] == "synthetic-latest.osm.pbf"
    assert rows[0]["region"] == "synthetic"
    assert rows[0]["osm_type"] == "way"
    assert rows[0]["osm_id"] == 99
    assert rows[0]["osm_version"] == 4
    assert rows[0]["candidate_kind"] == "closed_way"
    assert rows[0]["rejection_kind"] == "untracked_candidate"
    assert rows[0]["message"] == "area callback fired without a prior candidate record"


def test_way_callback_preserves_rejection_and_candidate_metadata() -> None:
    rows: list[dict[str, object]] = []
    recorded: list[tuple[object, ...]] = []
    handler: Any = object.__new__(_ExtractionHandler)
    handler._source_pbf = "synthetic-latest.osm.pbf"
    handler._region = "synthetic"
    handler.rej_sink = cast(BatchParquetSink, SimpleNamespace(add=rows.append))
    handler._record_candidate = lambda *args: recorded.append(args)
    open_way: Any = SimpleNamespace(
        tags=[("website", "https://example.org"), ("name", "Open")],
        nodes=[SimpleNamespace(ref=1), SimpleNamespace(ref=2), SimpleNamespace(ref=3)],
        id=10,
        version=4,
        timestamp=dt.datetime(2024, 1, 1),
    )

    handler.way(cast(osmium.osm.Way, open_way))

    assert rows[0]["source_pbf"] == "synthetic-latest.osm.pbf"
    assert rows[0]["region"] == "synthetic"
    assert rows[0]["osm_type"] == "way"
    assert rows[0]["osm_id"] == 10
    assert rows[0]["osm_version"] == 4
    assert rows[0]["osm_timestamp"] == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert rows[0]["candidate_kind"] == "closed_way"
    assert rows[0]["rejection_kind"] == "open_way_with_website"
    assert rows[0]["message"] == "open way with a qualifying website/wikidata tag"

    closed_way: Any = SimpleNamespace(
        tags=open_way.tags,
        nodes=[
            SimpleNamespace(ref=1),
            SimpleNamespace(ref=2),
            SimpleNamespace(ref=3),
            SimpleNamespace(ref=1),
        ],
        id=11,
        version=5,
        timestamp=dt.datetime(2024, 1, 1),
    )
    handler.way(cast(osmium.osm.Way, closed_way))

    assert recorded == [
        (
            "way",
            11,
            {"website": "https://example.org", "name": "Open"},
            5,
            dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            "closed_way",
        )
    ]


def test_relation_callback_filters_unsupported_and_records_wikidata() -> None:
    recorded: list[tuple[object, ...]] = []
    handler: Any = object.__new__(_ExtractionHandler)
    handler._record_candidate = lambda *args: recorded.append(args)
    relation: Any = SimpleNamespace(
        tags=[("type", "multipolygon"), ("wikidata", "Q42")],
        id=20,
        version=6,
        timestamp=dt.datetime(2024, 1, 1),
    )
    unsupported: Any = SimpleNamespace(
        tags=[("type", "route"), ("website", "https://example.org")],
        id=21,
        version=7,
        timestamp=dt.datetime(2024, 1, 1),
    )

    handler.relation(cast(osmium.osm.Relation, relation))
    handler.relation(cast(osmium.osm.Relation, unsupported))

    assert recorded == [
        (
            "relation",
            20,
            {"type": "multipolygon", "wikidata": "Q42"},
            6,
            dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            "relation_polygon",
        )
    ]
