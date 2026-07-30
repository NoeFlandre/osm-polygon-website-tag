"""Tests for verify_results."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.run_state import (
    initialise_run,
    record_processed_source,
    snapshot_source_fingerprint,
)
from osm_polygon_website_tag.verify import VerificationReport, verify_results


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row(
    *,
    polygon_id: str,
    region: str,
    source_pbf: str,
    lat: float = 0.0,
    lon: float = 0.0,
    area_m2: float = 50.0,
    website: str = "https://example.com",
    contact_website: str | None = None,
    has_website: bool = True,
    has_contact_website: bool = False,
    has_any_website: bool = True,
    preferred_website: str = "https://example.com",
    preferred_website_source: str = "website",
    website_class: str = "absolute_url",
    contact_website_class: str | None = None,
    website_hostname: str = "example.com",
    contact_website_hostname: str | None = None,
    wikidata: str | None = "Q42",
    wikidata_qid: str | None = "Q42",
    wikidata_class: str | None = "canonical_qid",
    name: str | None = None,
    tags: str = "{}",
    tag_keys: str = "[]",
    tag_count: int = 0,
    osm_primary_tag: str = "building",
    geometry: str = json.dumps({"type": "Polygon", "coordinates": []}),
    centroid: str = json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
    bbox: str = "[0.0,0.0,0.0,0.0]",
    area_km2: float = 5e-5,
    area_bucket: str = "10-100m2",
    centroid_kind: str = "lambert_azimuthal_equal_area",
    schema_version: str = "v1.2",
    website_text: str | None = "example text",
    website_word_count: int | None = 2,
    website_text_status: str = "success",
    contact_website_text: str | None = None,
    contact_website_word_count: int | None = None,
    contact_website_text_status: str = "absent",
) -> dict[str, object]:
    return {
        "polygon_id": polygon_id,
        "region": region,
        "source_pbf": source_pbf,
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "website": website,
        "contact_website": contact_website,
        "has_website": has_website,
        "has_contact_website": has_contact_website,
        "has_any_website": has_any_website,
        "preferred_website": preferred_website,
        "preferred_website_source": preferred_website_source,
        "website_class": website_class,
        "contact_website_class": contact_website_class,
        "website_hostname": website_hostname,
        "contact_website_hostname": contact_website_hostname,
        "wikidata": wikidata,
        "wikidata_qid": wikidata_qid,
        "wikidata_class": wikidata_class,
        "name": name,
        "tags": tags,
        "tag_keys": tag_keys,
        "tag_count": tag_count,
        "osm_primary_tag": osm_primary_tag,
        "geometry": geometry,
        "centroid": centroid,
        "lat": lat,
        "lon": lon,
        "bbox": bbox,
        "area_m2": area_m2,
        "area_km2": area_km2,
        "area_bucket": area_bucket,
        "centroid_kind": centroid_kind,
        "schema_version": schema_version,
        "website_text": website_text,
        "website_word_count": website_word_count,
        "website_text_status": website_text_status,
        "contact_website_text": contact_website_text,
        "contact_website_word_count": contact_website_word_count,
        "contact_website_text_status": contact_website_text_status,
    }


def _make_shard(run_dir: Path, *, stem: str, rows: list[dict[str, object]], kind: str) -> Path:
    """kind in {public, comparison, rejection}."""
    if kind == "public":
        schema = POLYGON_PUBLIC_SCHEMA
        parent = run_dir / "polygons"
    elif kind == "comparison":
        schema = COMPARISON_OBSERVATION_SCHEMA
        parent = run_dir / "analysis_observations"
    else:
        schema = REJECTION_SCHEMA
        parent = run_dir / "rejections"
    parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    p = parent / f"{stem}.parquet"
    pq.write_table(table, p, compression="snappy")
    return p


def _setup_minimal_run(tmp_path: Path, *, row_count: int = 1, manifest_row_count: int = 1):
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    rows = [
        _row(polygon_id="p1", region="monaco", source_pbf="monaco-latest.osm.pbf")
        for _ in range(row_count)
    ]
    shard = _make_shard(run_dir, stem="monaco-latest", rows=rows, kind="public")
    observation_shard = _make_shard(run_dir, stem="monaco-latest", rows=[], kind="comparison")
    rejection_shard = _make_shard(run_dir, stem="monaco-latest", rows=[], kind="rejection")
    public_sha = _sha256(shard)
    record_processed_source(
        state,
        fp,
        public_row_count=manifest_row_count,
        observation_row_count=0,
        rejection_count=0,
        public_shard_sha256=public_sha,
        observation_shard_sha256=_sha256(observation_shard),
        rejection_shard_sha256=_sha256(rejection_shard),
    )
    return run_dir, state


def _sha256(p: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def test_verify_results_happy_path(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    report = verify_results(run_dir)
    assert isinstance(report, VerificationReport)
    assert report.ok is True
    assert report.errors == []


def test_verify_rejects_incorrect_website_word_count(tmp_path: Path) -> None:
    from osm_polygon_website_tag.run_state import update_public_shard_metadata

    run_dir, state = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    rows = pq.read_table(shard).to_pylist()
    rows[0]["website_word_count"] = 99
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), shard)
    update_public_shard_metadata(
        state,
        filename="monaco-latest.osm.pbf",
        row_count=1,
        shard_sha256=_sha256(shard),
    )

    report = verify_results(run_dir)

    assert not report.ok
    assert any("word count" in error for error in report.errors)


def test_verify_results_rejects_modified_shard(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    new = table.to_pylist()
    new[0]["website"] = "https://tampered.com"
    pq.write_table(pa.Table.from_pylist(new, schema=table.schema), shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("mismatch" in e or "count" in e or "modified" in e for e in report.errors)


def test_verify_rejects_modified_zero_row_observation_shard(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "analysis_observations" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    pq.write_table(table, shard, compression="gzip")

    report = verify_results(run_dir)

    assert report.ok is False
    assert any("comparison" in error and "hash" in error for error in report.errors)


def test_verify_results_rejects_missing_shard(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(
        state, fp, public_row_count=1, observation_row_count=0, rejection_count=0
    )
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("missing" in e.lower() for e in report.errors)


def test_verify_results_rejects_extra_shard(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(
        state, fp, public_row_count=1, observation_row_count=0, rejection_count=0
    )
    rows = [_row(polygon_id="p1", region="monaco", source_pbf="monaco-latest.osm.pbf")]
    _make_shard(run_dir, stem="monaco-latest", rows=rows, kind="public")
    _make_shard(run_dir, stem="monaco-latest", rows=[], kind="comparison")
    _make_shard(run_dir, stem="monaco-latest", rows=[], kind="rejection")
    # Add rogue shard:
    _make_shard(run_dir, stem="rogue", rows=rows, kind="public")
    _make_shard(run_dir, stem="rogue", rows=[], kind="comparison")
    _make_shard(run_dir, stem="rogue", rows=[], kind="rejection")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("extra" in e.lower() or "undeclared" in e.lower() for e in report.errors)


def test_verify_results_rejects_schema_drift(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard).drop(["name"])
    pq.write_table(table, shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("schema" in e.lower() for e in report.errors)


def test_verify_results_rejects_row_count_mismatch(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path, row_count=1, manifest_row_count=5)
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("row" in e.lower() for e in report.errors)


def test_verify_results_rejects_empty_manifest(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    rows = [_row(polygon_id="p1", region="monaco", source_pbf="monaco-latest.osm.pbf")]
    _make_shard(run_dir, stem="monaco-latest", rows=rows, kind="public")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("manifest" in e.lower() or "empty" in e.lower() for e in report.errors)


def test_verify_results_rejects_nan_coordinates(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    new = table.to_pylist()
    new[0]["lat"] = float("nan")
    pq.write_table(pa.Table.from_pylist(new, schema=table.schema), shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False


def test_verify_results_rejects_negative_area(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    new = table.to_pylist()
    new[0]["area_m2"] = -1.0
    pq.write_table(pa.Table.from_pylist(new, schema=table.schema), shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False


def test_verify_results_rejects_corrupt_manifest(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    (run_dir / "manifests" / "sources.json").write_text("{not-json")
    report = verify_results(run_dir)
    assert report.ok is False


def test_verify_results_math_isfinite_helper() -> None:
    assert math.isfinite(1.0)
    assert not math.isfinite(float("inf"))
    assert not math.isfinite(float("nan"))
