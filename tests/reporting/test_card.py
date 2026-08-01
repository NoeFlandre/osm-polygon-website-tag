"""Tests for build_card."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
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
    pq.write_table(pa.Table.from_pylist([], schema=REJECTION_SCHEMA), rej, compression="snappy")
    return run_dir


def test_build_card_writes_readme_and_yaml(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    path = build_card(run_dir)
    assert path.exists()
    assert (run_dir / "dataset.yaml").exists()
    content = path.read_text()
    assert content.startswith("---")
    assert "license: odbl" in content
    assert "license_name:" not in content
    assert "task_categories:" not in content
    assert "task_categories:" not in (run_dir / "dataset.yaml").read_text()
    assert "size_categories:\n  - n<1K" in content
    assert "© OpenStreetMap contributors" in content
    assert "https://www.openstreetmap.org/copyright" in content
    assert "https://download.geofabrik.de/" in content


def test_build_card_writes_h3_density_map_and_card_section(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)

    build_card(run_dir)

    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    card = (run_dir / "README.md").read_text()
    assert map_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "## Geographic distribution" in card
    assert "assets/geographic_polygon_density.png" in card
    assert "H3 resolution 3" in card


def test_build_card_embeds_observation_count(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    path = build_card(run_dir)
    content = path.read_text()
    assert "| Public polygons | 1 |" in content
    assert "| Source PBFs | 1 / 1 |" in content
    assert "`website` OR `contact:website`" in content
    assert "| `polygon_id` |" in content
    assert "| `contact_website` |" in content
    assert "| `preferred_website` |" not in content
    assert "| `wikidata` |" not in content
    assert "| `area_km2` |" not in content


def test_build_card_links_detailed_analysis_instead_of_embedding_it(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    analyze_results(run_dir)
    path = build_card(run_dir)
    content = path.read_text()
    assert "Eight-cell provenance cube" not in content
    assert "Per-source coverage" not in content
    assert "`analysis/*.parquet`" in content


def test_card_stats_populates_canonical_count_from_analysis(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "osm_type": "way",
                    "osm_id": 100,
                    "osm_version": 1,
                    "osm_timestamp": _ts(),
                    "source_pbf": "monaco-latest.osm.pbf",
                    "region": "monaco",
                    "primary_category": "building",
                    "website": "https://example.com",
                    "contact_website": None,
                    "wikidata": None,
                    "has_website": True,
                    "has_contact_website": False,
                    "has_any_website": True,
                    "has_wikidata": False,
                    "schema_version": "v1.1",
                }
            ],
            schema=COMPARISON_OBSERVATION_SCHEMA,
        ),
        run_dir / "analysis_observations" / "monaco-latest.parquet",
    )
    analyze_results(run_dir)

    stats = compute_card_stats(run_dir)

    assert stats.canonical_count == 1


def test_card_stats_fails_closed_on_corrupt_parquet(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    (run_dir / "polygons" / "monaco-latest.parquet").write_bytes(b"corrupt")

    with pytest.raises(pa.ArrowInvalid):
        compute_card_stats(run_dir)


def test_build_card_is_idempotent(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    p1 = build_card(run_dir).read_text()
    p2 = build_card(run_dir).read_text()
    assert p1 == p2


@pytest.mark.parametrize(
    ("row_count", "expected"),
    [
        (0, "n<1K"),
        (999, "n<1K"),
        (1_000, "1K<n<10K"),
        (9_999, "1K<n<10K"),
        (10_000, "10K<n<100K"),
        (100_000, "100K<n<1M"),
        (1_000_000, "1M<n<10M"),
        (10_000_000, "10M<n<100M"),
        (100_000_000, "100M<n<1B"),
        (1_000_000_000, "n>1B"),
    ],
)
def test_size_category_is_derived_from_public_row_count(row_count: int, expected: str) -> None:
    from osm_polygon_website_tag.reporting.card import _size_category

    assert _size_category(row_count) == expected


def test_card_stats_derives_text_and_word_totals_from_polygon_parquet(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    row = _public_row()
    row.update(
        {
            "schema_version": "v1.2",
            "website_text": "one two three",
            "website_word_count": 3,
            "website_text_status": "success",
            "contact_website_text": None,
            "contact_website_word_count": None,
            "contact_website_text_status": "absent",
        }
    )
    pq.write_table(
        pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "monaco-latest.parquet",
    )

    stats = compute_card_stats(run_dir)

    assert stats.expected_sources_count == 1
    assert stats.enriched_sources_count == 1
    assert stats.website_urls_present == 1
    assert stats.website_text_success_count == 1
    assert stats.website_total_words == 3
    assert stats.contact_website_urls_present == 0
    assert stats.polygons_with_any_text == 1


def test_card_stats_can_scope_to_uploaded_sources(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    second = _public_row(polygon_id="p2", source_pbf="france-latest.osm.pbf")
    pq.write_table(
        pa.Table.from_pylist([second], schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "france-latest.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA),
        run_dir / "analysis_observations" / "france-latest.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([], schema=REJECTION_SCHEMA),
        run_dir / "rejections" / "france-latest.parquet",
    )

    stats = compute_card_stats(run_dir, source_names={"monaco-latest.osm.pbf"})

    assert stats.sources_count == 1
    assert stats.public_row_count == 1
    assert stats.observation_count == 0


def test_incremental_card_renders_progress_and_text_statistics(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    (run_dir / "manifests" / "expected_sources.json").write_text(
        json.dumps(
            [
                {"filename": "monaco-latest.osm.pbf", "size_bytes": 1, "mtime_ns": 1},
                {"filename": "france-latest.osm.pbf", "size_bytes": 1, "mtime_ns": 1},
            ]
        )
    )
    row = _public_row()
    row.update(
        {
            "schema_version": "v1.2",
            "website_text": "one two three",
            "website_word_count": 3,
            "website_text_status": "success",
            "contact_website_text": None,
            "contact_website_word_count": None,
            "contact_website_text_status": "absent",
        }
    )
    pq.write_table(
        pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "monaco-latest.parquet",
    )

    content = build_card(run_dir).read_text()

    assert "dataset_status: in_progress" in content
    assert "| Source PBFs | 1 / 2 |" in content
    assert "| `website` | 1 | 1 | 0 | 0 | 3 |" in content
    assert "Combined extracted words: **3**" in content
    assert "Trafilatura" in content
    assert "Unicode `\\w+`" in content


def test_hostname_renderer_caps_public_table_at_ten_rows() -> None:
    from osm_polygon_website_tag.reporting.card import _render_hostnames

    rows = [
        {"website_hostname": f"host-{index}.example", "row_count": 20 - index}
        for index in range(11)
    ]

    rendered = _render_hostnames(
        "website",
        rows,
        hostname_key="website_hostname",
    )

    assert "host-9.example" in rendered
    assert "host-10.example" not in rendered
