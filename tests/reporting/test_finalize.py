"""Tests for finalize_run."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.finalize import finalize_run
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
    transition_status,
)


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row(
    *, polygon_id: str = "p1", region: str = "monaco", source_pbf: str = "monaco-latest.osm.pbf"
):
    return {
        "polygon_id": polygon_id,
        "region": region,
        "source_pbf": source_pbf,
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "website": "https://example.com",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "preferred_website": "https://example.com",
        "preferred_website_source": "website",
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.com",
        "contact_website_hostname": None,
        "wikidata": "Q42",
        "wikidata_qid": "Q42",
        "wikidata_class": "canonical_qid",
        "name": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": "building",
        "geometry": json.dumps({"type": "Polygon", "coordinates": []}),
        "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0.0,0.0,0.0,0.0]",
        "area_m2": 50.0,
        "area_km2": 5e-5,
        "area_bucket": "10-100m2",
        "centroid_kind": "lambert_azimuthal_equal_area",
        "schema_version": "v1.2",
        "website_text": "example text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _setup(tmp_path: Path) -> tuple[Path, object]:
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    run_dir, state = initialise_run(tmp_path, run_id="r", expected_sources=[fp])
    rows = [_row()]
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pub.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), pub, compression="snappy"
    )
    obs = run_dir / "analysis_observations" / "monaco-latest.parquet"
    obs.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA),
        obs,
        compression="snappy",
    )
    rej = run_dir / "rejections" / "monaco-latest.parquet"
    rej.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([], schema=REJECTION_SCHEMA), rej, compression="snappy")
    record_processed_source(
        state,
        fp,
        public_row_count=1,
        observation_row_count=0,
        rejection_count=0,
        public_shard_sha256=_sha(pub),
        observation_shard_sha256=_sha(obs),
        rejection_shard_sha256=_sha(rej),
    )
    # Walk through the state machine.
    transition_status(state, STATUS_EXTRACTING)
    transition_status(state, STATUS_EXTRACTED)
    transition_status(state, STATUS_ENRICHING)
    transition_status(state, STATUS_ENRICHED)
    analyze_results(run_dir)
    transition_status(state, STATUS_ANALYZED)
    build_card(run_dir)
    transition_status(state, STATUS_CARD_BUILT)
    return run_dir, state


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_finalize_run_writes_receipt(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    report = finalize_run(run_dir)
    assert report.ok is True
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text())
    assert "manifest_digest" in receipt
    assert receipt["sources_count"] == 1
    paths = {entry["path"] for entry in receipt["artifacts"]}
    assert "README.md" in paths
    assert "analysis/cells_global.parquet" in paths
    assert "polygons/monaco-latest.parquet" in paths


def test_finalize_run_transitions_to_complete(tmp_path: Path) -> None:
    run_dir, _state = _setup(tmp_path)
    finalize_run(run_dir)
    state = load_run(run_dir)
    assert state.metadata["status"] == STATUS_COMPLETE


def test_finalize_run_receipt_digest_is_stable(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    r1 = finalize_run(run_dir)
    # Re-finalize -- receipt is rewritten deterministically.
    r2 = finalize_run(run_dir)
    assert r1.receipt["manifest_digest"] == r2.receipt["manifest_digest"]


def test_finalize_run_fails_on_verification_error(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    # Corrupt the shard.
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    shard.write_bytes(b"not parquet")
    report = finalize_run(run_dir)
    assert report.ok is False


def test_finalize_run_can_proceed_to_complete(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    finalize_run(run_dir)
    state = load_run(run_dir)
    assert state.metadata["status"] == STATUS_COMPLETE


def test_complete_verification_rejects_card_mutation(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    assert finalize_run(run_dir).ok
    (run_dir / "README.md").write_text("tampered")

    report = verify_results(run_dir)

    assert not report.ok
    assert any("README" in error or "receipt-bound" in error for error in report.errors)


def test_complete_verification_rejects_analysis_mutation(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    assert finalize_run(run_dir).ok
    path = run_dir / "analysis" / "cells_global.parquet"
    path.write_bytes(path.read_bytes() + b"tampered")

    report = verify_results(run_dir)

    assert not report.ok
    assert any("analysis" in error or "receipt-bound" in error for error in report.errors)
