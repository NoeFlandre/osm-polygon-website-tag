"""Tests for the analyze module (DuckDB-backed, v1.1 contract)."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.pipeline.analyze import (
    ANALYSIS_FILES,
    analyze_results,
)
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.runtime.run_state import initialise_run


def _write_comparison_shard(
    run_dir: Path,
    *,
    stem: str,
    rows: list[dict[str, object]],
) -> Path:
    path = run_dir / "analysis_observations" / f"{stem}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=COMPARISON_OBSERVATION_SCHEMA)
    pq.write_table(table, path, compression="snappy")
    return path


def _write_public_shard(
    run_dir: Path,
    *,
    stem: str,
    rows: list[dict[str, object]],
) -> Path:
    path = run_dir / "polygons" / f"{stem}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA)
    pq.write_table(table, path, compression="snappy")
    return path


def _write_rejection_shard(
    run_dir: Path,
    *,
    stem: str,
    rows: list[dict[str, object]],
) -> Path:
    path = run_dir / "rejections" / f"{stem}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=REJECTION_SCHEMA)
    pq.write_table(table, path, compression="snappy")
    return path


def _make_minimal_run(tmp_path: Path) -> Path:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    return run_dir


def _ts() -> object:
    import pyarrow as pa

    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row_obs(
    *,
    osm_id: int,
    osm_type: str = "way",
    source_pbf: str = "monaco-latest.osm.pbf",
    region: str = "monaco",
    primary_category: str = "building",
    website: str | None = None,
    contact_website: str | None = None,
    wikidata: str | None = None,
) -> dict[str, object]:
    has_ws = website is not None and website != ""
    has_cw = contact_website is not None and contact_website != ""
    has_wd = wikidata is not None and wikidata != ""
    return {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "source_pbf": source_pbf,
        "region": region,
        "primary_category": primary_category,
        "website": website,
        "contact_website": contact_website,
        "wikidata": wikidata,
        "has_website": has_ws,
        "has_contact_website": has_cw,
        "has_any_website": has_ws or has_cw,
        "has_wikidata": has_wd,
        "schema_version": "v1.2",
        "website_text": "text",
        "website_word_count": 1,
        "website_text_status": "success",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def test_analyze_results_writes_eight_cell_global_table(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[
            _row_obs(osm_id=1, website="https://x.com"),
            _row_obs(osm_id=2, contact_website="https://y.com"),
            _row_obs(osm_id=3, wikidata="Q1"),
            _row_obs(osm_id=4, website="https://a.com", wikidata="Q2"),
        ],
    )
    analyze_results(run_dir)
    p = run_dir / "analysis" / "cells_global.parquet"
    assert p.exists()
    rows = pq.read_table(p).to_pylist()
    # 8 cells x 2 levels = 16 rows.
    assert len(rows) == 16
    cell_100_obs = next(
        r["row_count"]
        for r in rows
        if r["cell"] == "cell_100_w1_c0_d0" and r["level"] == "observation"
    )
    assert cell_100_obs == 1


def test_analyze_results_eight_cells_sum_to_observation_population(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    rows = [
        _row_obs(osm_id=1, website="https://x.com"),
        _row_obs(osm_id=2, contact_website="https://y.com"),
        _row_obs(osm_id=3, wikidata="Q1"),
        _row_obs(osm_id=4, website="https://a.com", wikidata="Q2"),
        _row_obs(osm_id=5, website="https://b.com", contact_website="https://c.com"),
    ]
    _write_comparison_shard(run_dir, stem="monaco-latest", rows=rows)
    summary = analyze_results(run_dir)
    assert sum(summary.cell_observation.values()) == 5
    assert summary.cell_observation["cell_000_w0_c0_d0"] == 0
    assert summary.cell_observation["cell_100_w1_c0_d0"] == 1
    assert summary.cell_observation["cell_010_w0_c1_d0"] == 1
    assert summary.cell_observation["cell_001_w0_c0_d1"] == 1
    assert summary.cell_observation["cell_101_w1_c0_d1"] == 1
    assert summary.cell_observation["cell_110_w1_c1_d0"] == 1


def test_analyze_results_canonical_dedup_via_row_number(make_pbf, tmp_path: Path) -> None:
    src = make_pbf(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://a.com"/>
  </way>
</osm>
""",
        name="monaco-latest.osm.pbf",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pbf = next(src.iterdir())
    extract_pbf(pbf, run_dir)
    # Add a duplicate observation for the same object via a second
    # synthetic comparison shard (still same source -- simulating a
    # second PBF that overlaps).
    from osm_polygon_website_tag.contracts.comparison_schema import (
        COMPARISON_OBSERVATION_SCHEMA_VERSION,
    )

    _write_comparison_shard(
        run_dir,
        stem="rhone-alpes-latest",
        rows=[
            {
                "osm_type": "way",
                "osm_id": 100,
                "osm_version": 2,
                "osm_timestamp": _ts(),
                "source_pbf": "rhone-alpes-latest.osm.pbf",
                "region": "rhone-alpes",
                "primary_category": "building",
                "website": "https://a.com",
                "contact_website": None,
                "wikidata": None,
                "has_website": True,
                "has_contact_website": False,
                "has_any_website": True,
                "has_wikidata": False,
                "schema_version": COMPARISON_OBSERVATION_SCHEMA_VERSION,
            }
        ],
    )
    summary = analyze_results(run_dir)
    # Two observations, one canonical.
    assert summary.observation_count == 2
    assert summary.canonical_count == 1
    assert summary.duplicate_count == 1


def test_analyze_results_canonical_winner_is_highest_version(make_pbf, tmp_path: Path) -> None:
    src_a = make_pbf(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://old.com"/>
  </way>
</osm>
""",
        name="monaco-latest.osm.pbf",
    )
    src_b = make_pbf(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="3" timestamp="2024-01-02T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://new.com"/>
  </way>
</osm>
""",
        name="rhone-alpes-latest.osm.pbf",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extract_pbf(next(src_a.iterdir()), run_dir)
    extract_pbf(next(src_b.iterdir()), run_dir)
    summary = analyze_results(run_dir)
    assert summary.canonical_count == 1
    # The winner is the highest-version observation.
    top_hostnames = pq.read_table(
        run_dir / "analysis" / "top_hostnames_website.parquet"
    ).to_pylist()
    assert top_hostnames[0]["website_hostname"] == "new.com"


def test_analyze_results_conflicting_snapshots_detected(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[
            _row_obs(
                osm_id=1,
                website="https://a.com",
                wikidata="Q1",
                source_pbf="monaco-latest.osm.pbf",
            )
        ],
    )
    _write_comparison_shard(
        run_dir,
        stem="rhone-alpes-latest",
        rows=[
            _row_obs(
                osm_id=1,
                website="https://b.com",  # conflicting website
                wikidata="Q1",
                source_pbf="rhone-alpes-latest.osm.pbf",
            )
        ],
    )
    summary = analyze_results(run_dir)
    assert summary.conflicting_snapshot_count == 1
    conf = pq.read_table(run_dir / "analysis" / "conflicting_snapshots.parquet").to_pylist()
    assert conf[0]["osm_type"] == "way"
    assert conf[0]["osm_id"] == 1


def test_analyze_results_writes_duplicate_observations(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[_row_obs(osm_id=1), _row_obs(osm_id=1, source_pbf="rhone-alpes-latest.osm.pbf")],
    )
    summary = analyze_results(run_dir)
    assert summary.duplicate_count == 1
    p = run_dir / "analysis" / "duplicate_observations.parquet"
    assert p.exists()


def test_analyze_results_writes_hostnames_exact_and_top(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[
            _row_obs(osm_id=1, website="https://a.com"),
            _row_obs(osm_id=2, website="https://a.com"),
            _row_obs(osm_id=3, contact_website="https://b.com"),
        ],
    )
    analyze_results(run_dir)
    website_exact = pq.read_table(
        run_dir / "analysis" / "hostnames_exact_website.parquet"
    ).to_pylist()
    contact_exact = pq.read_table(
        run_dir / "analysis" / "hostnames_exact_contact_website.parquet"
    ).to_pylist()
    website_top = pq.read_table(run_dir / "analysis" / "top_hostnames_website.parquet").to_pylist()
    assert any(r["website_hostname"] == "a.com" and r["row_count"] == 2 for r in website_exact)
    assert any(r["contact_website_hostname"] == "b.com" for r in contact_exact)
    assert website_top[0]["website_hostname"] == "a.com"


def test_hostname_analysis_accepts_bare_and_scheme_relative_values(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[
            _row_obs(osm_id=1, website="Example.COM/path"),
            _row_obs(osm_id=2, contact_website="//Contact.Example/path"),
        ],
    )

    analyze_results(run_dir)

    website = pq.read_table(run_dir / "analysis" / "hostnames_exact_website.parquet").to_pylist()
    contact = pq.read_table(
        run_dir / "analysis" / "hostnames_exact_contact_website.parquet"
    ).to_pylist()
    assert website == [{"website_hostname": "example.com", "row_count": 1}]
    assert contact == [{"contact_website_hostname": "contact.example", "row_count": 1}]


def test_analysis_orchestrator_does_not_fetch_unbounded_result_sets() -> None:
    source = inspect.getsource(analyze_results)
    assert ".fetchall(" not in source
    assert ".to_pylist(" not in source


def test_analyze_results_writes_class_counts(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[
            _row_obs(osm_id=1, website="https://a.com"),
            _row_obs(osm_id=2, contact_website="https://b.com"),
        ],
    )
    analyze_results(run_dir)
    p = run_dir / "analysis" / "by_website_class_canonical.parquet"
    assert p.exists()


def test_analyze_results_writes_per_group_cells(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[_row_obs(osm_id=1, website="https://a.com")],
    )
    _write_comparison_shard(
        run_dir,
        stem="rhone-alpes-latest",
        rows=[_row_obs(osm_id=2, contact_website="https://b.com", region="rhone-alpes")],
    )
    analyze_results(run_dir)
    assert (run_dir / "analysis" / "cells_by_source.parquet").exists()
    assert (run_dir / "analysis" / "cells_by_region.parquet").exists()
    assert (run_dir / "analysis" / "cells_by_primary_category.parquet").exists()


def test_analyze_results_no_shards_returns_zero_summary(tmp_path: Path) -> None:
    """A run directory with empty (schema-valid) shards still produces
    a valid AnalysisSummary with all-zero counts."""
    run_dir, _ = initialise_run(tmp_path, run_id="empty")
    summary = analyze_results(run_dir)
    assert summary.observation_count == 0
    assert summary.canonical_count == 0
    assert summary.public_row_count == 0
    assert summary.rejection_count == 0
    assert summary.duplicate_count == 0
    assert summary.conflicting_snapshot_count == 0
    for k in summary.cell_observation:
        assert summary.cell_observation[k] == 0


def test_analyze_results_emits_stable_summary(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[_row_obs(osm_id=1, website="https://a.com")],
    )
    a = analyze_results(run_dir)
    b = analyze_results(run_dir)
    # Summary structure is deterministic.
    assert a.observation_count == b.observation_count
    assert a.cell_observation == b.cell_observation


def test_analysis_promotion_failure_preserves_previous_bundle(tmp_path: Path, monkeypatch) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[_row_obs(osm_id=1, website="https://a.example")],
    )
    analyze_results(run_dir)
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (run_dir / "analysis").glob("*.parquet")
    }

    def fail_promotion(_promotions):
        raise OSError("injected analysis promotion failure")

    monkeypatch.setattr(
        "osm_polygon_website_tag.pipeline.analyze.atomic_promote_bundle",
        fail_promotion,
    )
    with pytest.raises(OSError, match="injected"):
        analyze_results(run_dir)

    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (run_dir / "analysis").glob("*.parquet")
    }
    assert after == before


def test_analyze_results_writes_by_source_overlap(tmp_path: Path) -> None:
    run_dir = _make_minimal_run(tmp_path)
    _write_public_shard(
        run_dir,
        stem="monaco-latest",
        rows=[
            {
                "polygon_id": "monaco-latest:way/100",
                "region": "monaco",
                "source_pbf": "monaco-latest.osm.pbf",
                "osm_type": "way",
                "osm_id": 100,
                "osm_version": 1,
                "osm_timestamp": _ts(),
                "name": None,
                "website": "https://x.com",
                "contact_website": None,
                "has_website": True,
                "has_contact_website": False,
                "has_any_website": True,
                "website_class": "absolute_url",
                "contact_website_class": None,
                "website_hostname": "x.com",
                "contact_website_hostname": None,
                "preferred_website": "https://x.com",
                "preferred_website_source": "website",
                "wikidata": None,
                "wikidata_qid": None,
                "wikidata_class": None,
                "tags": "{}",
                "tag_keys": "[]",
                "tag_count": 0,
                "osm_primary_tag": "building",
                "geometry": json.dumps({"type": "Polygon", "coordinates": []}),
                "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
                "centroid_kind": "lambert_azimuthal_equal_area",
                "lat": 0.0,
                "lon": 0.0,
                "bbox": "[0.0,0.0,0.0,0.0]",
                "area_m2": 0.0,
                "area_km2": 0.0,
                "area_bucket": "<10m2",
                "schema_version": "v1.2",
                "website_text": "text",
                "website_word_count": 1,
                "website_text_status": "success",
                "contact_website_text": None,
                "contact_website_word_count": None,
                "contact_website_text_status": "absent",
            }
        ],
    )
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[_row_obs(osm_id=100)],
    )
    analyze_results(run_dir)
    p = run_dir / "analysis" / "by_source_overlap.parquet"
    table = pq.read_table(p)
    assert table.num_rows == 1


def test_analyze_results_handles_very_large_repeated_identity(tmp_path: Path) -> None:
    """1000 identical-key observations must be handled in bounded memory."""
    run_dir = _make_minimal_run(tmp_path)
    rows = [
        _row_obs(
            osm_id=1,
            website="https://x.com",
            source_pbf=f"s-{i:04d}.osm.pbf",
        )
        for i in range(1000)
    ]
    # Write to one big shard (batched writes handled by extract_pbf).
    _write_comparison_shard(run_dir, stem="big", rows=rows)
    summary = analyze_results(run_dir)
    assert summary.observation_count == 1000
    assert summary.canonical_count == 1


def test_analyze_results_swallows_duckdb_with_low_memory(monkeypatch, tmp_path: Path) -> None:
    """Even with a tiny DuckDB memory budget the analysis succeeds."""
    monkeypatch.setattr(
        "osm_polygon_website_tag.storage.duckdb_engine.DEFAULT_MEMORY_LIMIT", "32MB"
    )
    run_dir = _make_minimal_run(tmp_path)
    _write_comparison_shard(
        run_dir,
        stem="monaco-latest",
        rows=[_row_obs(osm_id=i, website=f"https://h{i}.com") for i in range(100)],
    )
    summary = analyze_results(run_dir)
    assert summary.observation_count == 100


def test_analysis_files_constant_lists_every_file() -> None:
    assert "cells_global.parquet" in ANALYSIS_FILES
    assert "cells_by_source.parquet" in ANALYSIS_FILES
    assert "cells_by_region.parquet" in ANALYSIS_FILES
    assert "cells_by_osm_type.parquet" in ANALYSIS_FILES
    assert "cells_by_primary_category.parquet" in ANALYSIS_FILES
    assert "by_website_class_canonical.parquet" in ANALYSIS_FILES
    assert "by_contact_website_class_canonical.parquet" in ANALYSIS_FILES
    assert "by_source_overlap.parquet" in ANALYSIS_FILES
    assert "by_source_dedup.parquet" in ANALYSIS_FILES
    assert "duplicate_observations.parquet" in ANALYSIS_FILES
    assert "conflicting_snapshots.parquet" in ANALYSIS_FILES
    assert "rejections_by_kind.parquet" in ANALYSIS_FILES
    assert "hostnames_exact_website.parquet" in ANALYSIS_FILES
    assert "hostnames_exact_contact_website.parquet" in ANALYSIS_FILES
    assert "top_hostnames_website.parquet" in ANALYSIS_FILES
    assert "top_hostnames_contact_website.parquet" in ANALYSIS_FILES
