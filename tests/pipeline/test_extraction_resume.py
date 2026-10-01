"""Tests for the per-PBF polygon extraction."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from tests.fixtures.extraction import (
    pbf_path as _pbf_path,
)

from osm_polygon_website_tag.pipeline.extraction import (
    ExtractFailure,
    _ExtractionHandler,
    extract_pbf,
)
from osm_polygon_website_tag.runtime.run_state import load_run


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

    original = _ExtractionHandler.area

    def boom(self, a):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced crash")

    _ExtractionHandler.area = boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            extract_pbf(src, run_dir, run_state=state)
    finally:
        _ExtractionHandler.area = original  # type: ignore[assignment]
    failure_line = (run_dir / "failures.jsonl").read_text(encoding="utf-8").strip()
    failure = json.loads(failure_line)
    assert list(failure) == sorted(failure)
    assert failure["source_pbf"] == "crash-latest.osm.pbf"
    assert failure["osm_type"] == ""
    assert failure["osm_id"] == 0
    assert failure["phase"] == "extract"
    assert failure["kind"] == "RuntimeError"
    assert failure["message"] == "forced crash"
    assert isinstance(failure["timestamp"], str)
    assert dt.datetime.fromisoformat(failure["timestamp"]).tzinfo == dt.UTC
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
