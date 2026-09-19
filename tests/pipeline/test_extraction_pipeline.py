"""Tests for the per-PBF polygon extraction."""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import osmium
import osmium.osm
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.extraction as extraction_module
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.domain.geometry import GeometryRejection
from osm_polygon_website_tag.pipeline import extraction_handler as extraction_handler_module
from osm_polygon_website_tag.pipeline.area_work import (
    AreaPayload,
    AreaResult,
    AreaWorkCoordinator,
    validate_area_settings,
)
from osm_polygon_website_tag.pipeline.extraction import (
    _ExtractionHandler,
    extract_pbf,
)
from osm_polygon_website_tag.pipeline.record_builders import DerivedTags
from osm_polygon_website_tag.runtime.run_state import SourceFingerprint

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


def test_process_area_payload_preserves_derived_values_and_geometry_fields() -> None:
    payload = _website_payload(
        '{"type":"Polygon","coordinates":[[[0.0,0.0],[0.01,0.0],[0.01,0.01],[0.0,0.0]]]}'
    )
    assert payload.derived_tags is not None
    derived = replace(payload.derived_tags, website="https://derived.example")
    result = extraction_handler_module._process_area_payload(replace(payload, derived_tags=derived))

    assert result.public_row is not None
    assert result.public_row["website"] == "https://derived.example"
    assert result.public_row["bbox"] != "null"
    assert result.observation_row is not None
    assert result.observation_row["website"] == "https://derived.example"


def test_process_area_payload_derives_missing_tags_projection() -> None:
    payload = _website_payload(
        '{"type":"Polygon","coordinates":[[[0.0,0.0],[0.01,0.0],[0.01,0.01],[0.0,0.0]]]}'
    )
    result = extraction_handler_module._process_area_payload(replace(payload, derived_tags=None))

    assert result.public_row is not None
    assert result.public_row["website"] == "https://example.org"
    assert result.observation_row is not None
    assert result.observation_row["has_any_website"] is True


def test_geometry_rejection_preserves_payload_and_derived_values() -> None:
    payload = _website_payload("not-used")
    assert payload.derived_tags is not None
    derived = replace(payload.derived_tags, website="https://derived.example")

    result = extraction_handler_module._geometry_rejection(
        payload,
        derived,
        "geometry_error",
        "broken geometry",
    )

    assert result.rejection_row == {
        "osm_type": "way",
        "osm_id": 1,
        "osm_version": 1,
        "osm_timestamp": dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        "source_pbf": "synthetic-latest.osm.pbf",
        "region": "synthetic",
        "primary_category": "building",
        "website": "https://derived.example",
        "contact_website": None,
        "wikidata": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "has_wikidata": False,
        "candidate_kind": "closed_way",
        "rejection_kind": "geometry_error",
        "message": "broken geometry",
        "schema_version": "v1.1",
    }


def test_load_geometry_converts_expected_and_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _website_payload("not-used")
    derived = payload.derived_tags
    assert derived is not None

    def raise_geometry_rejection(_raw: str):
        raise GeometryRejection("antimeridian", "crosses antimeridian")

    monkeypatch.setattr(
        extraction_handler_module, "geometry_from_geojson", raise_geometry_rejection
    )
    custom_derived = replace(derived, website="https://derived.example")
    rejected = extraction_handler_module._load_geometry(payload, custom_derived)
    assert isinstance(rejected, AreaResult)
    assert rejected.rejection_row is not None
    assert rejected.rejection_row["rejection_kind"] == "antimeridian"
    assert rejected.rejection_row["message"] == "crosses antimeridian"
    assert rejected.rejection_row["website"] == "https://derived.example"

    def raise_unexpected(_raw: str):
        raise RuntimeError("broken geometry")

    monkeypatch.setattr(extraction_handler_module, "geometry_from_geojson", raise_unexpected)
    failed = extraction_handler_module._load_geometry(payload, custom_derived)
    assert isinstance(failed, AreaResult)
    assert failed.rejection_row is not None
    assert failed.rejection_row["rejection_kind"] == "geometry_error"
    assert failed.rejection_row["message"] == "RuntimeError: broken geometry"
    assert failed.rejection_row["website"] == "https://derived.example"


def test_build_public_result_preserves_derived_values_on_geometry_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _website_payload("not-used")
    derived = payload.derived_tags
    assert derived is not None
    custom_derived = replace(derived, website="https://derived.example")

    def raise_geometry_rejection(_raw: str) -> None:
        raise GeometryRejection("antimeridian", "crosses antimeridian")

    monkeypatch.setattr(
        extraction_handler_module, "geometry_from_geojson", raise_geometry_rejection
    )

    result = extraction_handler_module._build_public_result(payload, custom_derived)

    assert isinstance(result, AreaResult)
    assert result.rejection_row is not None
    assert result.rejection_row["website"] == "https://derived.example"
    assert result.rejection_row["rejection_kind"] == "antimeridian"


@pytest.mark.parametrize(
    ("factory_error", "expected_kind", "expected_message"),
    [
        (
            GeometryRejection("antimeridian", "crosses antimeridian"),
            "antimeridian",
            "crosses antimeridian",
        ),
        (RuntimeError("factory failed"), "geometry_error", "RuntimeError: factory failed"),
    ],
)
def test_serialize_area_geometry_records_factory_failures(
    monkeypatch: pytest.MonkeyPatch,
    factory_error: Exception,
    expected_kind: str,
    expected_message: str,
) -> None:
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        _ExtractionHandler,
        "_drain_area_work",
        lambda _self: events.append(("drain", "")),
    )
    monkeypatch.setattr(
        _ExtractionHandler,
        "_flush_geometry_rejection",
        lambda _self, area, kind, message: events.append(("reject", area, kind, message)),
    )

    class Factory:
        def create_multipolygon(self, _area):
            raise factory_error

    monkeypatch.setattr(extraction_handler_module.osmium.geom, "GeoJSONFactory", Factory)

    handler = object.__new__(_ExtractionHandler)
    area = object()
    result = handler._serialize_area_geometry(cast(osmium.osm.Area, area))

    assert result is None
    assert events[0] == ("drain", "")
    assert events[1] == ("reject", area, expected_kind, expected_message)


def test_serialize_area_geometry_forwards_area_to_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    class Factory:
        def create_multipolygon(self, area: object) -> str:
            seen.append(area)
            return '{"type":"Polygon"}'

    monkeypatch.setattr(extraction_handler_module.osmium.geom, "GeoJSONFactory", Factory)

    handler = object.__new__(_ExtractionHandler)
    area = object()
    result = handler._serialize_area_geometry(cast(osmium.osm.Area, area))

    assert result == '{"type":"Polygon"}'
    assert seen == [area]


def test_area_worker_reuses_precomputed_tag_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The area worker must not re-derive tags already projected by the callback."""
    from osm_polygon_website_tag.pipeline import extraction_handler

    payload_type = getattr(extraction_module, "AreaPayload", None)
    if payload_type is None:
        pytest.fail("AreaPayload is not implemented")
    derived = DerivedTags(
        website="https://example.org",
        contact_website=None,
        has_website=True,
        has_contact_website=False,
        has_any_website=True,
        primary_category="building",
    )
    payload = payload_type(
        sequence=1,
        source_pbf="synthetic-latest.osm.pbf",
        region="synthetic",
        tags_dict={"website": "https://example.org", "building": "yes"},
        osm_type="way",
        osm_id=1,
        osm_version=1,
        osm_timestamp=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        candidate_kind="closed_way",
        raw_geojson=(
            '{"type":"Polygon","coordinates":[[[0.0,0.0],[0.01,0.0],[0.01,0.01],[0.0,0.0]]]}'
        ),
        derived_tags=derived,
    )

    def fail_derive(_tags: dict[str, str]) -> DerivedTags:
        pytest.fail("area worker re-derived a precomputed tag projection")

    monkeypatch.setattr(extraction_handler, "derive_tags", fail_derive)
    result = extraction_module._process_area_payload(payload)

    assert result.public_row is not None
    assert result.public_row["website"] == "https://example.org"
    assert result.observation_row is not None
    assert result.observation_row["has_any_website"] is True


def test_extraction_preserves_area_work_compatibility_surface() -> None:
    assert extraction_module.AreaPayload is AreaPayload
    assert extraction_module.AreaResult is AreaResult
    assert extraction_module._AreaWorkCoordinator is AreaWorkCoordinator
    assert extraction_module._validate_area_settings is validate_area_settings


def test_extraction_handler_implementation_has_a_focused_module() -> None:
    from osm_polygon_website_tag.pipeline import extraction_handler

    assert extraction_module._ExtractionHandler.__module__ == extraction_handler.__name__
    assert extraction_module._process_area_payload.__module__ == extraction_handler.__name__


def test_extract_worker_counts_produce_identical_shards(
    synthetic_source_simple: Path, tmp_path: Path
) -> None:
    assert "area_workers" in inspect.signature(extract_pbf).parameters
    run_single = tmp_path / "single"
    run_parallel = tmp_path / "parallel"
    run_single.mkdir()
    run_parallel.mkdir()

    extract_pbf(
        synthetic_source_simple,
        run_single,
        area_workers=1,
        max_in_flight_areas=2,
    )
    extract_pbf(
        synthetic_source_simple,
        run_parallel,
        area_workers=3,
        max_in_flight_areas=6,
    )

    for directory in ("polygons", "analysis_observations", "rejections"):
        single = next((run_single / directory).glob("*.parquet"))
        parallel = next((run_parallel / directory).glob("*.parquet"))
        assert (
            hashlib.sha256(single.read_bytes()).digest()
            == hashlib.sha256(parallel.read_bytes()).digest()
        )


def test_source_mutation_fails_before_shard_promotion(
    synthetic_source_simple: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = SourceFingerprint(
        filename=synthetic_source_simple.name,
        size_bytes=synthetic_source_simple.stat().st_size,
        mtime_ns=1,
    )
    after = SourceFingerprint(
        filename=synthetic_source_simple.name,
        size_bytes=synthetic_source_simple.stat().st_size,
        mtime_ns=2,
    )
    snapshots = iter((before, after))
    monkeypatch.setattr(
        "osm_polygon_website_tag.pipeline.extraction.snapshot_source_fingerprint",
        lambda _path: next(snapshots),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(RuntimeError, match="source changed"):
        extract_pbf(synthetic_source_simple, run_dir)

    assert not list((run_dir / "polygons").glob("*.parquet"))
    assert not list((run_dir / "analysis_observations").glob("*.parquet"))
    assert not list((run_dir / "rejections").glob("*.parquet"))
    assert not list(run_dir.rglob("*.sqlite3"))


def test_extract_empty_shards_are_schema_valid(make_pbf, tmp_path: Path) -> None:
    """An empty source produces schema-valid empty Parquet files in
    all three locations."""
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/></osm>
""",
            name="empty-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    pub = pq.read_table(run_dir / "polygons" / "empty-latest.parquet")
    assert pub.schema.equals(POLYGON_PUBLIC_SCHEMA)
    assert pub.num_rows == 0
    cmp_ = pq.read_table(run_dir / "analysis_observations" / "empty-latest.parquet")
    assert cmp_.schema.equals(COMPARISON_OBSERVATION_SCHEMA)
    assert cmp_.num_rows == 0
    rej = pq.read_table(run_dir / "rejections" / "empty-latest.parquet")
    assert rej.schema.equals(REJECTION_SCHEMA)
    assert rej.num_rows == 0


def test_extract_writes_pbf_files_only_for_provided_pbf(tmp_path: Path) -> None:
    """Passing a directory to the per-PBF API must raise ValueError."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError):
        extract_pbf(tmp_path, run_dir)


def test_extract_polygon_id_format(synthetic_source_simple: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    ids = table["polygon_id"].to_pylist()
    assert ids == ["monaco-latest:way/100"]


def test_extract_excludes_open_way(synthetic_source_simple: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    ids = table["polygon_id"].to_pylist()
    assert "monaco-latest:way/101" not in ids


def test_extract_excludes_polygon_without_any_website(
    synthetic_source_simple: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    ids = table["polygon_id"].to_pylist()
    # Way 102 has no website key at all -- excluded.
    assert "monaco-latest:way/102" not in ids


def test_extract_includes_geometry(synthetic_source_simple: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    geom_text = table["geometry"][0].as_py()
    parsed = json.loads(geom_text)
    assert parsed["type"] == "Polygon"


def test_extract_preserves_original_trimmed_values(
    synthetic_source_simple: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert table["website"][0].as_py() == "https://example.com"
    assert table["name"][0].as_py() == "Building A"
    observations = pq.read_table(run_dir / "analysis_observations" / "monaco-latest.parquet")
    assert observations["wikidata"][0].as_py() == "Q42"


def test_extract_writes_to_run_owned_dir(synthetic_source_simple: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "my-run-id"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    assert (run_dir / "polygons" / "monaco-latest.parquet").exists()
    assert not (synthetic_source_simple.parent / "polygons").exists()
