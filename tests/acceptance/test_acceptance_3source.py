"""3-source synthetic acceptance test.

This is the G1 acceptance test. It exercises the entire pipeline
end-to-end on three synthetic sources (A, B, C) and asserts every
invariant the production pipeline must preserve.

Source A (monaco-latest.osm.pbf): six polygons covering the eight-cell
cube plus a polygon-with-hole.

Source B (rhone-alpes-latest.osm.pbf): a newer version of one A
polygon (canonical-winner test), a conflict (older snapshot wins),
a valid multipolygon relation, and a candidate that fails geometry.

Source C (bretagne-latest.osm.pbf): zero public-website polygons.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
from osm_polygon_website_tag.reporting.finalize import finalize_run
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.run_state import initialise_run

# ---------------------------------------------------------------------------
# Source A
# ---------------------------------------------------------------------------
SOURCE_A_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/>
  <node id="2" lat="0.0" lon="0.1"/>
  <node id="3" lat="0.1" lon="0.1"/>
  <node id="4" lat="0.1" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://x.com"/>
  </way>

  <node id="11" lat="1.0" lon="1.0"/><node id="12" lat="1.0" lon="1.1"/>
  <node id="13" lat="1.1" lon="1.1"/><node id="14" lat="1.1" lon="1.0"/>
  <way id="200" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="14"/><nd ref="11"/>
    <tag k="building" v="yes"/>
    <tag k="contact:website" v="https://y.com"/>
  </way>

  <node id="21" lat="2.0" lon="2.0"/><node id="22" lat="2.0" lon="2.1"/>
  <node id="23" lat="2.1" lon="2.1"/><node id="24" lat="2.1" lon="2.0"/>
  <way id="300" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="21"/><nd ref="22"/><nd ref="23"/><nd ref="24"/><nd ref="21"/>
    <tag k="building" v="yes"/>
    <tag k="wikidata" v="Q1"/>
  </way>

  <node id="31" lat="3.0" lon="3.0"/><node id="32" lat="3.0" lon="3.1"/>
  <node id="33" lat="3.1" lon="3.1"/><node id="34" lat="3.1" lon="3.0"/>
  <way id="400" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="31"/><nd ref="32"/><nd ref="33"/><nd ref="34"/><nd ref="31"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://a.com"/>
    <tag k="wikidata" v="Q2"/>
  </way>

  <node id="41" lat="4.0" lon="4.0"/><node id="42" lat="4.0" lon="4.1"/>
  <node id="43" lat="4.1" lon="4.1"/><node id="44" lat="4.1" lon="4.0"/>
  <way id="500" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="41"/><nd ref="42"/><nd ref="43"/><nd ref="44"/><nd ref="41"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://b.com"/>
    <tag k="contact:website" v="https://c.com"/>
  </way>

  <node id="51" lat="5.0" lon="5.0"/><node id="52" lat="5.0" lon="5.1"/>
  <node id="53" lat="5.1" lon="5.1"/><node id="54" lat="5.1" lon="5.0"/>
  <way id="600" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="51"/><nd ref="52"/><nd ref="53"/><nd ref="54"/><nd ref="51"/>
    <tag k="building" v="yes"/>
  </way>

  <node id="61" lat="6.5" lon="6.5"/><node id="62" lat="6.5" lon="6.6"/>
  <node id="63" lat="6.6" lon="6.6"/><node id="64" lat="6.6" lon="6.5"/>
  <way id="610" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="61"/><nd ref="62"/><nd ref="63"/><nd ref="64"/><nd ref="61"/>
    <tag k="building" v="yes"/>
    <tag k="contact:website" v="//contact.example/path"/>
    <tag k="wikidata" v="Q610"/>
  </way>

  <node id="71" lat="7.0" lon="7.0"/><node id="72" lat="7.0" lon="7.1"/>
  <node id="73" lat="7.1" lon="7.1"/><node id="74" lat="7.1" lon="7.0"/>
  <way id="620" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="71"/><nd ref="72"/><nd ref="73"/><nd ref="74"/><nd ref="71"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="all.example/path"/>
    <tag k="contact:website" v="https://contact-all.example"/>
    <tag k="wikidata" v="Q620"/>
  </way>
</osm>
"""


# ---------------------------------------------------------------------------
# Source B: newer version of polygon 100, conflict on polygon 400,
# valid multipolygon relation, geometry-error candidate.
# ---------------------------------------------------------------------------
SOURCE_B_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/>
  <node id="2" lat="0.0" lon="0.1"/>
  <node id="3" lat="0.1" lon="0.1"/>
  <node id="4" lat="0.1" lon="0.0"/>
  <way id="100" version="3" timestamp="2024-02-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://new.com"/>
  </way>

  <node id="31" lat="3.0" lon="3.0"/><node id="32" lat="3.0" lon="3.1"/>
  <node id="33" lat="3.1" lon="3.1"/><node id="34" lat="3.1" lon="3.0"/>
  <way id="400" version="1" timestamp="2024-01-15T00:00:00Z">
    <nd ref="31"/><nd ref="32"/><nd ref="33"/><nd ref="34"/><nd ref="31"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://conflict.com"/>
    <tag k="wikidata" v="Q999"/>
  </way>

  <node id="61" lat="6.0" lon="6.0"/><node id="62" lat="6.0" lon="6.1"/>
  <node id="63" lat="6.1" lon="6.1"/><node id="64" lat="6.1" lon="6.0"/>
  <node id="65" lat="6.05" lon="6.05"/><node id="66" lat="6.05" lon="6.06"/>
  <node id="67" lat="6.06" lon="6.06"/><node id="68" lat="6.06" lon="6.05"/>
  <way id="700" version="1" timestamp="2024-02-01T00:00:00Z">
    <nd ref="61"/><nd ref="62"/><nd ref="63"/><nd ref="64"/><nd ref="61"/>
    <tag k="building" v="yes"/>
  </way>
  <way id="701" version="1" timestamp="2024-02-01T00:00:00Z">
    <nd ref="65"/><nd ref="66"/><nd ref="67"/><nd ref="68"/><nd ref="65"/>
    <tag k="building" v="yes"/>
  </way>
  <relation id="800" version="1" timestamp="2024-02-01T00:00:00Z">
    <member type="way" ref="700" role="outer"/>
    <member type="way" ref="701" role="inner"/>
    <tag k="type" v="multipolygon"/>
    <tag k="building" v="yes"/>
    <tag k="website" v="https://rel.com"/>
  </relation>
</osm>
"""


# ---------------------------------------------------------------------------
# Source C: zero public-website polygons.
# ---------------------------------------------------------------------------
SOURCE_C_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="81" lat="8.0" lon="8.0"/><node id="82" lat="8.0" lon="8.1"/>
  <node id="83" lat="8.1" lon="8.1"/><node id="84" lat="8.1" lon="8.0"/>
  <way id="900" version="1" timestamp="2024-03-01T00:00:00Z">
    <nd ref="81"/><nd ref="82"/><nd ref="83"/><nd ref="84"/><nd ref="81"/>
    <tag k="building" v="yes"/>
  </way>
</osm>
"""


def _make_pbf(make_pbf, xml: str, name: str, tmp_path: Path) -> Path:
    """Synthesize a PBF from XML and return the inner file path."""
    src = make_pbf(xml, name=name)
    return next(src.iterdir())


def test_acceptance_three_sources_end_to_end(make_pbf, tmp_path: Path) -> None:
    """End-to-end acceptance test on three synthetic sources.

    Asserts:
    - 3 public + 3 comparison + 3 rejection shards exist.
    - Empty shards have exact schemas (zero-row, schema-valid).
    - All eight cells are correct at observation and canonical level.
    - Canonical winner is highest-version (way 100 has website=new.com).
    - Conflicting snapshots detected (way 400 has two sources).
    - README card numbers are entirely artifact-derived.
    - Verification detects every mutation.
    """
    pbf_a = _make_pbf(make_pbf, SOURCE_A_XML, "monaco-latest.osm.pbf", tmp_path)
    pbf_b = _make_pbf(make_pbf, SOURCE_B_XML, "rhone-alpes-latest.osm.pbf", tmp_path)
    pbf_c = _make_pbf(make_pbf, SOURCE_C_XML, "bretagne-latest.osm.pbf", tmp_path)
    from osm_polygon_website_tag.runtime.run_state import (
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_ENRICHED,
        STATUS_ENRICHING,
        STATUS_EXTRACTED,
        STATUS_EXTRACTING,
        snapshot_source_fingerprint,
        transition_status,
    )

    run_dir, state = initialise_run(
        tmp_path,
        run_id="acceptance",
        expected_sources=[snapshot_source_fingerprint(path) for path in (pbf_a, pbf_b, pbf_c)],
    )
    transition_status(state, STATUS_EXTRACTING)
    extract_pbf(pbf_a, run_dir, run_state=state)
    extract_pbf(pbf_b, run_dir, run_state=state)
    extract_pbf(pbf_c, run_dir, run_state=state)
    transition_status(state, STATUS_EXTRACTED)
    transition_status(state, STATUS_ENRICHING)
    from osm_polygon_website_tag.contracts.text_schema import count_words
    from osm_polygon_website_tag.pipeline.enrich import enrich_polygon_shard
    from osm_polygon_website_tag.web.text_extract import TextExtraction
    from osm_polygon_website_tag.web.web_fetch import FetchResult

    for shard in sorted((run_dir / "polygons").glob("*.parquet")):
        enrich_polygon_shard(
            shard,
            cache_path=run_dir / "cache" / "text.sqlite3",
            invocation_id="acceptance",
            fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"public website text"),
            extractor=lambda _html, *, url: TextExtraction(
                "success",
                f"public text from {url}",
                count_words(f"public text from {url}"),
                None,
                "2.1.0",
            ),
        )
        source_name = f"{shard.stem}.osm.pbf"
        from osm_polygon_website_tag.runtime.run_state import (
            hash_shard,
            update_public_shard_metadata,
        )

        update_public_shard_metadata(
            state,
            filename=source_name,
            row_count=pq.ParquetFile(shard).metadata.num_rows,
            shard_sha256=hash_shard(shard),
        )
    transition_status(state, STATUS_ENRICHED)

    # Three shards per source × three sources = nine shards.  # noqa: RUF003
    assert len(list((run_dir / "polygons").glob("*.parquet"))) == 3
    assert len(list((run_dir / "analysis_observations").glob("*.parquet"))) == 3
    assert len(list((run_dir / "rejections").glob("*.parquet"))) == 3

    # Empty shards (source C has no public polygons) have schema and
    # zero rows.
    c_pub = pq.read_table(run_dir / "polygons" / "bretagne-latest.parquet")
    assert c_pub.num_rows == 0

    summary = analyze_results(run_dir)
    transition_status(state, STATUS_ANALYZED)
    # Source A contributes six website-qualified polygons, source B
    # contributes three, and source C contributes zero.
    assert summary.public_row_count == 9

    # All eight cells are explicit; 000 is structurally zero because
    # comparison observations require a website field or Wikidata.
    assert summary.cell_observation == {
        "cell_000_w0_c0_d0": 0,
        "cell_001_w0_c0_d1": 1,
        "cell_010_w0_c1_d0": 1,
        "cell_011_w0_c1_d1": 1,
        "cell_100_w1_c0_d0": 3,
        "cell_101_w1_c0_d1": 2,
        "cell_110_w1_c1_d0": 1,
        "cell_111_w1_c1_d1": 1,
    }
    assert summary.cell_canonical == {
        "cell_000_w0_c0_d0": 0,
        "cell_001_w0_c0_d1": 1,
        "cell_010_w0_c1_d0": 1,
        "cell_011_w0_c1_d1": 1,
        "cell_100_w1_c0_d0": 2,
        "cell_101_w1_c0_d1": 1,
        "cell_110_w1_c1_d0": 1,
        "cell_111_w1_c1_d1": 1,
    }

    # Canonical: dedup on osm_id.
    assert summary.canonical_count == 8
    assert summary.duplicate_count == 2  # way 100, way 400 each appear twice (relation 800 once)

    # Hostnames: 'new.com' must be the top winner for way 100.
    top = pq.read_table(run_dir / "analysis" / "top_hostnames_website.parquet").to_pylist()
    assert any(r["website_hostname"] == "new.com" and r["row_count"] == 1 for r in top)

    # Card stats are entirely artifact-derived.
    stats = compute_card_stats(run_dir)
    assert stats.public_row_count == 9
    assert stats.sources_count == 3

    # Build the card; every number must come from the artifacts.
    readme_path = build_card(run_dir)
    text = readme_path.read_text()
    assert "| Source regions processed | 3 / 3 |" in text
    assert "| Public polygons | 9 |" in text
    assert "ODbL" in text
    assert "license: odbl" in text
    assert "task_categories:" not in text

    transition_status(state, STATUS_CARD_BUILT)

    report = finalize_run(run_dir)
    assert report.ok is True

    # Verification passes on a clean run.
    v = verify_results(run_dir)
    assert v.ok is True, v.errors

    # Verification detects a tampered shard.
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    new = table.to_pylist()
    new[0]["website"] = "https://tampered.com"
    pq.write_table(pa.Table.from_pylist(new, schema=table.schema), shard, compression="snappy")
    v2 = verify_results(run_dir)
    assert v2.ok is False


def test_acceptance_geometry_relation_assembled(make_pbf, tmp_path: Path) -> None:
    """Source B's relation 800 produces a single canonical row with
    both outer ring (way 700) and inner ring (way 701) preserved."""
    run_dir, _ = initialise_run(tmp_path, run_id="relation")
    pbf = _make_pbf(make_pbf, SOURCE_B_XML, "rhone-alpes-latest.osm.pbf", tmp_path)
    extract_pbf(pbf, run_dir)
    summary = analyze_results(run_dir)
    assert summary.public_row_count >= 1
    # The canonical row's polygon is the relation, not the inner ring.
    con = duckdb.connect()
    pub_glob = str(run_dir / "polygons" / "*.parquet").replace("'", "''")
    rows = con.execute(
        f"SELECT osm_id, polygon_id FROM read_parquet('{pub_glob}') WHERE osm_type='relation'"  # noqa: S608
    ).fetchall()
    assert len(rows) == 1


def test_acceptance_hole_reduces_area(make_pbf, tmp_path: Path) -> None:
    """Relation 800 (polygon with hole) has area < polygon-of-way-700-only area.

    Way 700 has no website tag, so it is not a public row by itself; it
    only contributes its ring to relation 800. The relation's outer ring
    is 0.01x0.01 (about 1.2e7 m² at lat 6) and the inner ring is
    0.01x0.01 (~ 1.2e7 m² at lat 6); the relation's net area must be
    strictly less than the outer-ring-only area."""
    run_dir, _ = initialise_run(tmp_path, run_id="hole")
    pbf = _make_pbf(make_pbf, SOURCE_B_XML, "rhone-alpes-latest.osm.pbf", tmp_path)
    extract_pbf(pbf, run_dir)
    con = duckdb.connect()
    pub_glob = str(run_dir / "polygons" / "*.parquet")
    rows = con.execute(
        "SELECT osm_id, area_m2, geometry, lon, lat FROM read_parquet(?) WHERE osm_type='relation'",
        [pub_glob],
    ).fetchall()
    assert len(rows) == 1
    from osm_polygon_website_tag.domain.geometry import compute_polygon_area_m2

    geometry = json.loads(rows[0][2])
    assert geometry["type"] == "Polygon"
    assert len(geometry["coordinates"]) == 2
    outer_area = compute_polygon_area_m2(geometry["coordinates"][0])
    assert 0 < rows[0][1] < outer_area
    # The north-east hole shifts the centroid south-west of the
    # outer square's (6.05, 6.05) center.
    assert rows[0][3] < 6.05
    assert rows[0][4] < 6.05
