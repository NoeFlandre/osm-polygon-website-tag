"""Tests for the per-PBF polygon extraction."""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
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
    ExtractFailure,
    ExtractionResult,
    _ExtractionHandler,
    extract_pbf,
)
from osm_polygon_website_tag.pipeline.record_builders import DerivedTags
from osm_polygon_website_tag.runtime.run_state import SourceFingerprint, load_run
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


def test_geometry_rejection_is_written_with_candidate_metadata() -> None:
    rows: list[dict[str, object]] = []
    handler = object.__new__(_ExtractionHandler)
    handler._source_pbf = "monaco-latest.osm.pbf"
    handler._region = "monaco"
    handler.rej_sink = cast(BatchParquetSink, SimpleNamespace(add=rows.append))
    area = SimpleNamespace(
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
    rejected = extraction_handler_module._load_geometry(payload, derived)
    assert isinstance(rejected, AreaResult)
    assert rejected.rejection_row is not None
    assert rejected.rejection_row["rejection_kind"] == "antimeridian"

    def raise_unexpected(_raw: str):
        raise RuntimeError("broken geometry")

    monkeypatch.setattr(extraction_handler_module, "geometry_from_geojson", raise_unexpected)
    failed = extraction_handler_module._load_geometry(payload, derived)
    assert isinstance(failed, AreaResult)
    assert failed.rejection_row is not None
    assert failed.rejection_row["rejection_kind"] == "geometry_error"


@pytest.mark.parametrize(
    ("factory_error", "expected_kind"),
    [
        (GeometryRejection("antimeridian", "crosses antimeridian"), "antimeridian"),
        (RuntimeError("factory failed"), "geometry_error"),
    ],
)
def test_serialize_area_geometry_records_factory_failures(
    monkeypatch: pytest.MonkeyPatch,
    factory_error: Exception,
    expected_kind: str,
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _ExtractionHandler,
        "_drain_area_work",
        lambda _self: events.append(("drain", "")),
    )
    monkeypatch.setattr(
        _ExtractionHandler,
        "_flush_geometry_rejection",
        lambda _self, _area, kind, _message: events.append(("reject", kind)),
    )

    class Factory:
        def create_multipolygon(self, _area):
            raise factory_error

    monkeypatch.setattr(extraction_handler_module.osmium.geom, "GeoJSONFactory", Factory)

    handler = object.__new__(_ExtractionHandler)
    result = handler._serialize_area_geometry(cast(osmium.osm.Area, object()))

    assert result is None
    assert events == [("drain", ""), ("reject", expected_kind)]


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


def test_extract_includes_contact_website_only_polygon(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="contact:website" v="https://contact.example"/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert table.num_rows == 1
    assert table["website"][0].as_py() is None
    assert table["contact_website"][0].as_py() == "https://contact.example"
    assert table["has_website"][0].as_py() is False
    assert table["has_contact_website"][0].as_py() is True
    assert table["has_any_website"][0].as_py() is True


def test_extract_includes_both_website_keys_preserving_both(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://primary.example"/>
    <tag k="contact:website" v="https://contact.example"/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert table.num_rows == 1
    assert table["website"][0].as_py() == "https://primary.example"
    assert table["contact_website"][0].as_py() == "https://contact.example"
    assert table["has_website"][0].as_py() is True
    assert table["has_contact_website"][0].as_py() is True


def test_extract_whitespace_only_website_with_valid_contact(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="   "/>
    <tag k="contact:website" v="https://x.example"/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert table.num_rows == 1
    assert table["has_website"][0].as_py() is False
    assert table["has_contact_website"][0].as_py() is True


def test_extract_does_not_confuse_contact_phone_with_contact_website(
    make_pbf, tmp_path: Path
) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="contact:phone" v="+33123456789"/>
    <tag k="contact:email" v="a@example.com"/>
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


def test_extract_assembles_multipolygon_relation(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
  </way>
  <relation id="200" version="1" timestamp="2024-01-01T00:00:00Z">
    <member type="way" ref="100" role="outer"/>
    <tag k="type" v="multipolygon"/>
    <tag k="landuse" v="forest"/>
    <tag k="website" v="https://forest.example"/>
  </relation>
</osm>
""",
            name="rhone-alpes-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = extract_pbf(src, run_dir)
    assert result.public_row_count == 1
    table = pq.read_table(run_dir / "polygons" / "rhone-alpes-latest.parquet")
    ids = table["polygon_id"].to_pylist()
    assert ids == ["rhone-alpes-latest:relation/200"]
    assert table["osm_type"][0].as_py() == "relation"
    parsed = json.loads(table["geometry"][0].as_py())
    # One polygon component without holes is Polygon, not MultiPolygon.
    assert parsed["type"] == "Polygon"


def test_extract_open_website_way_lands_in_rejections_not_failures(
    make_pbf, tmp_path: Path
) -> None:
    """Open ways with website are expected exclusions, not processing failures."""
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://example.com"/>
  </way>
</osm>
""",
            name="broken-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = extract_pbf(src, run_dir)
    assert result.public_row_count == 0
    # No failure log entry; rejections only.
    failure_log = run_dir / "failures.jsonl"
    if failure_log.exists():
        lines = failure_log.read_text().strip().splitlines()
        assert not any("open_way_with_website" in line for line in lines)
    rej = pq.read_table(run_dir / "rejections" / "broken-latest.parquet").to_pylist()
    kinds = [r["rejection_kind"] for r in rej]
    assert "open_way_with_website" in kinds


def test_extract_records_failure_for_extractor_crash(make_pbf, tmp_path: Path) -> None:
    """A genuine crash during extraction is a processing failure."""
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
  </way>
</osm>
""",
            name="crash-latest.osm.pbf",
        )
    )
    from osm_polygon_website_tag.runtime.run_state import initialise_run

    run_dir, state = initialise_run(tmp_path, run_id="run")
    # Force a failure by monkeypatching the area callback to raise.
    from osm_polygon_website_tag.pipeline.extraction import _ExtractionHandler

    original = _ExtractionHandler.area

    def boom(self, a):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced crash")

    _ExtractionHandler.area = boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            extract_pbf(src, run_dir, run_state=state)
    finally:
        _ExtractionHandler.area = original  # type: ignore[assignment]
    failure = json.loads((run_dir / "failures.jsonl").read_text())
    assert failure["source_pbf"] == "crash-latest.osm.pbf"
    assert failure["phase"] == "extract"
    assert load_run(run_dir).metadata["status"] == "incomplete"


def test_keyboard_interrupt_keeps_extracting_run_resumable(
    synthetic_source_simple: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.pipeline import extraction
    from osm_polygon_website_tag.runtime.run_state import (
        STATUS_EXTRACTING,
        initialise_run,
        transition_status,
    )

    run_dir, state = initialise_run(tmp_path, run_id="run")
    transition_status(state, STATUS_EXTRACTING)

    def interrupt(_handler: object, _path: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(extraction._ExtractionHandler, "apply_file", interrupt)
    with pytest.raises(KeyboardInterrupt):
        extract_pbf(synthetic_source_simple, run_dir, run_state=state)

    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING
    assert not (run_dir / "failures.jsonl").exists()


def test_extract_atomic_finalize(make_pbf, tmp_path: Path) -> None:
    """No partial files left behind in the polygons dir."""
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
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    final_path = run_dir / "polygons" / "monaco-latest.parquet"
    assert final_path.exists()
    leftovers = [
        p.name for p in (run_dir / "polygons").iterdir() if p.name != "monaco-latest.parquet"
    ]
    assert leftovers == []


def test_extraction_promotion_failure_preserves_previous_bundle(
    synthetic_source_simple: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(synthetic_source_simple, run_dir)
    before = {
        path.relative_to(run_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ("polygons", "analysis_observations", "rejections")
        for path in (run_dir / directory).glob("*.parquet")
    }

    def fail_promotion(_promotions):
        raise OSError("injected extraction promotion failure")

    monkeypatch.setattr(
        "osm_polygon_website_tag.pipeline.extraction.atomic_promote_bundle",
        fail_promotion,
    )
    with pytest.raises(OSError, match="injected"):
        extract_pbf(synthetic_source_simple, run_dir)

    after = {
        path.relative_to(run_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ("polygons", "analysis_observations", "rejections")
        for path in (run_dir / directory).glob("*.parquet")
    }
    assert after == before


def test_extract_trims_website_and_wikidata_whitespace(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="  https://example.com  "/>
    <tag k="wikidata" v=" Q42 "/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert table["website"][0].as_py() == "https://example.com"
    assert json.loads(table["tags"][0].as_py())["wikidata"] == " Q42 "


def test_extract_includes_tag_keys_and_tags_as_sorted_json(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="website" v="https://example.com"/>
    <tag k="building" v="yes"/>
    <tag k="name" v="Building A"/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    keys = json.loads(table["tag_keys"][0].as_py())
    assert keys == ["building", "name", "website"]
    tags = json.loads(table["tags"][0].as_py())
    assert tags == {"building": "yes", "name": "Building A", "website": "https://example.com"}


def test_extract_osm_primary_tag(make_pbf, tmp_path: Path) -> None:
    src = _pbf_path(
        make_pbf(
            """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="boundary" v="administrative"/>
    <tag k="website" v="https://example.com"/>
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert table["osm_primary_tag"][0].as_py() == "boundary"


def test_extract_failure_dataclass_is_immutable(tmp_path: Path) -> None:
    f = ExtractFailure(
        source_pbf="monaco-latest.osm.pbf",
        osm_type="way",
        osm_id=42,
        phase="area_assembly",
        kind="unassembled_geometry",
        message="missing nodes",
        timestamp="2024-01-01T00:00:00Z",
    )
    with pytest.raises((AttributeError, Exception)):
        f.kind = "x"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_extract_emits_schema_version(make_pbf, tmp_path: Path) -> None:
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
  </way>
</osm>
""",
            name="monaco-latest.osm.pbf",
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(src, run_dir)
    table = pq.read_table(run_dir / "polygons" / "monaco-latest.parquet")
    assert table["schema_version"][0].as_py() == "v1.3"


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
