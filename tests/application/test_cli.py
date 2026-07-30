"""Tests for the CLI dispatcher."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.application.cli import _build_parser, main
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.runtime.run_state import (
    hash_shard,
    initialise_run,
    record_processed_source,
    snapshot_source_fingerprint,
)


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row():
    return {
        "polygon_id": "p1",
        "region": "monaco",
        "source_pbf": "monaco-latest.osm.pbf",
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


def _setup_run(tmp_path: Path) -> Path:
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pub.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([_row()], schema=POLYGON_PUBLIC_SCHEMA), pub, compression="snappy"
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
        public_shard_sha256=hash_shard(pub),
        observation_shard_sha256=hash_shard(obs),
        rejection_shard_sha256=hash_shard(rej),
    )
    return run_dir


def test_cli_help_exits_2() -> None:
    rc = main([])
    assert rc == 2


def test_cli_init_records_exact_expected_sources(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "monaco-latest.osm.pbf"
    source.write_bytes(b"synthetic")
    output_root = tmp_path / "runs"

    rc = main(
        [
            "init",
            "--output-root",
            str(output_root),
            "--run-id",
            "r1",
            "--source-root",
            str(source_root),
            "--expected-source",
            str(source),
        ]
    )

    assert rc == 0
    manifest = json.loads((output_root / "r1" / "manifests" / "expected_sources.json").read_text())
    assert manifest == [
        {
            "filename": source.name,
            "mtime_ns": source.stat().st_mtime_ns,
            "size_bytes": source.stat().st_size,
        }
    ]


def test_cli_init_rejects_output_inside_source_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "monaco-latest.osm.pbf"
    source.write_bytes(b"synthetic")

    rc = main(
        [
            "init",
            "--output-root",
            str(source_root / "runs"),
            "--run-id",
            "unsafe",
            "--source-root",
            str(source_root),
            "--expected-source",
            str(source),
        ]
    )

    assert rc == 2
    assert not (source_root / "runs").exists()


def test_cli_rejects_hf_token_arguments() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["publish", "--run-dir", "/tmp/run", "--hf-token", "secret"])
    with pytest.raises(SystemExit):
        parser.parse_args(["create-repo", "--repo-id", "owner/name", "--hf-token", "secret"])


def test_cli_extract_preserves_real_counts(make_pbf, tmp_path: Path) -> None:
    source_dir = make_pbf(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
<node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="1"/>
<node id="3" lat="1" lon="1"/><node id="4" lat="1" lon="0"/>
<way id="10" version="1" timestamp="2024-01-01T00:00:00Z">
<nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/>
</way></osm>""",
        name="monaco-latest.osm.pbf",
    )
    source = next(source_dir.iterdir())
    source_root = source.parent
    output_root = tmp_path / "runs"
    assert (
        main(
            [
                "init",
                "--output-root",
                str(output_root),
                "--run-id",
                "r1",
                "--source-root",
                str(source_root),
                "--expected-source",
                str(source),
            ]
        )
        == 0
    )

    assert main(["extract", str(source), "--run-dir", str(output_root / "r1")]) == 0

    manifest = json.loads((output_root / "r1" / "manifests" / "sources.json").read_text())
    assert manifest[0]["public_row_count"] == 1
    assert manifest[0]["observation_row_count"] == 1
    metadata = json.loads((output_root / "r1" / "manifests" / "run.json").read_text())
    assert metadata["status"] == "extracted"


def test_cli_verify_results_returns_zero_on_pass(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["verify-results", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_verify_results_returns_nonzero_on_failure(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    (run_dir / "polygons" / "monaco-latest.parquet").write_bytes(b"junk")
    rc = main(["verify-results", "--run-dir", str(run_dir)])
    assert rc == 1


def test_cli_card_stats_runs(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["card-stats", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_publish_plan_runs(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["publish-plan", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_create_repo_requires_token() -> None:
    rc = main(["create-repo", "--repo-id", "foo/bar"])
    assert rc != 0


def test_cli_publish_dry_run(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["publish", "--run-dir", str(run_dir)])
    assert rc == 0
