"""Shared deterministic fixtures for dataset-card tests."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.reporting.card_stats import CardStats
from osm_polygon_website_tag.reporting.geometry_stats import (
    AreaStats,
    ExtentStats,
    GeometryStats,
    NumericSummary,
    ShapeStats,
)
from osm_polygon_website_tag.runtime.run_state import initialise_run


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _public_row(*, polygon_id: str = "p1", source_pbf: str = "monaco-latest.osm.pbf"):
    return {
        "polygon_id": polygon_id,
        "region": "monaco",
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
        "schema_version": "v1.1",
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "pending",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _setup_minimal_run(tmp_path: Path) -> Path:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pub.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([_public_row()], schema=POLYGON_PUBLIC_SCHEMA),
        pub,
        compression="snappy",
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
    pq.write_table(
        pa.Table.from_pylist([], schema=REJECTION_SCHEMA),
        rej,
        compression="snappy",
    )
    return run_dir


def _golden_geometry_stats() -> GeometryStats:
    return GeometryStats(
        row_count=25,
        area=AreaStats(
            summary=NumericSummary(
                row_count=25,
                total=2_500_000.0,
                minimum=26.0,
                maximum=27.0,
                mean=28.0,
                median=29.0,
                percentiles={"p95": 30.0},
            ),
            below_one_m2_row_count=31,
        ),
        shape=ShapeStats(multipolygon_row_count=32, with_holes_row_count=33),
        extent=ExtentStats(bbox=[-1.5, -2.5, 3.5, 4.5]),
    )


def _golden_card_stats() -> CardStats:
    return CardStats(
        snapshot_status="in_progress",
        observation_count=2,
        public_row_count=3,
        rejection_count=4,
        sources_count=5,
        expected_sources_count=6,
        duplicate_count=7,
        conflicting_snapshot_count=8,
        website_urls_present=9,
        website_text_success_count=10,
        website_text_empty_count=11,
        website_text_failure_count=12,
        website_total_words=13,
        contact_website_urls_present=14,
        contact_website_text_success_count=15,
        contact_website_text_empty_count=16,
        contact_website_text_failure_count=17,
        contact_website_total_words=18,
        polygons_with_any_text=19,
        polygon_density_h3_resolution=20,
        occupied_h3_cell_count=21,
        polygon_density_row_count=22,
        top_hostnames_website=[{"website_hostname": "example.org", "row_count": 23}],
        top_hostnames_contact_website=[
            {"contact_website_hostname": "contact.example", "row_count": 24}
        ],
    )


def _language_card_stats() -> CardStats:
    stats = _golden_card_stats()
    stats.detected_language_count = 2
    stats.website_language_count = 10
    stats.contact_website_language_count = 15
    stats.top_languages = [("eng_Latn", 25), ("deu_Latn", 5)]
    return stats


def _sentence_card_stats() -> CardStats:
    stats = _language_card_stats()
    stats.total_sentence_count = 60
    stats.website_sentence_row_count = 8
    stats.contact_website_sentence_row_count = 4
    stats.unsupported_language_row_count = 3
    return stats
