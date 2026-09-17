"""Global website-text population contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.text_population import (
    TextCoordinate,
    compute_text_population_summary,
    iter_canonical_text_coordinates,
)


def _row(
    *,
    osm_id: int,
    lat: float,
    lon: float,
    source_pbf: str,
    polygon_id: str,
    osm_version: int,
    website_text: str | None,
    website_status: str,
    website_words: int | None,
    contact_text: str | None,
    contact_status: str,
    contact_words: int | None,
) -> dict[str, object]:
    return {
        "osm_type": "way",
        "osm_id": osm_id,
        "osm_version": osm_version,
        "osm_timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "source_pbf": source_pbf,
        "polygon_id": polygon_id,
        "lat": lat,
        "lon": lon,
        "website": "https://example.org" if website_text else None,
        "contact_website": "https://contact.example.org" if contact_text else None,
        "website_text": website_text,
        "website_text_status": website_status,
        "website_word_count": website_words,
        "contact_website_text": contact_text,
        "contact_website_text_status": contact_status,
        "contact_website_word_count": contact_words,
        "website_language": "eng" if website_text else None,
        "contact_website_language": "fra" if contact_text else None,
    }


def _write_run(root: Path, rows: list[dict[str, object]], *, split: bool) -> None:
    polygons = root / "polygons"
    polygons.mkdir(parents=True)
    if split:
        midpoint = len(rows) // 2
        pq.write_table(pa.Table.from_pylist(rows[:midpoint]), polygons / "b.parquet")
        pq.write_table(pa.Table.from_pylist(rows[midpoint:]), polygons / "a.parquet")
    else:
        pq.write_table(pa.Table.from_pylist(rows), polygons / "a.parquet")


def test_population_uses_one_deterministic_winner_per_qualifying_identity(
    tmp_path: Path,
) -> None:
    rows = [
        _row(
            osm_id=42,
            lat=40.0,
            lon=2.0,
            source_pbf="z.osm.pbf",
            polygon_id="z:way/42",
            osm_version=1,
            website_text="old copy",
            website_status="success",
            website_words=2,
            contact_text=None,
            contact_status="absent",
            contact_words=None,
        ),
        _row(
            osm_id=42,
            lat=48.85,
            lon=2.35,
            source_pbf="a.osm.pbf",
            polygon_id="a:way/42",
            osm_version=2,
            website_text="new copy",
            website_status="success",
            website_words=2,
            contact_text=None,
            contact_status="absent",
            contact_words=None,
        ),
        _row(
            osm_id=43,
            lat=40.7,
            lon=-74.0,
            source_pbf="a.osm.pbf",
            polygon_id="a:way/43",
            osm_version=1,
            website_text=None,
            website_status="absent",
            website_words=None,
            contact_text="contact copy",
            contact_status="success",
            contact_words=3,
        ),
        _row(
            osm_id=44,
            lat=35.0,
            lon=139.0,
            source_pbf="a.osm.pbf",
            polygon_id="a:way/44",
            osm_version=1,
            website_text="   ",
            website_status="success",
            website_words=0,
            contact_text=None,
            contact_status="absent",
            contact_words=None,
        ),
    ]
    _write_run(tmp_path, rows, split=True)

    population = compute_text_population_summary(tmp_path)

    assert population.unique_identity_count == 2
    assert population.website_identity_count == 1
    assert population.contact_website_identity_count == 1
    assert population.website_total_words == 2
    assert population.contact_website_total_words == 3
    assert list(iter_canonical_text_coordinates(tmp_path)) == [
        TextCoordinate(
            osm_type="way",
            osm_id=42,
            lat=48.85,
            lon=2.35,
            source_path=tmp_path / "polygons" / "b.parquet",
        ),
        TextCoordinate(
            osm_type="way",
            osm_id=43,
            lat=40.7,
            lon=-74.0,
            source_path=tmp_path / "polygons" / "a.parquet",
            row_index=1,
        ),
    ]


def test_population_is_independent_of_file_and_row_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    rows = [
        _row(
            osm_id=42,
            lat=48.85,
            lon=2.35,
            source_pbf="z.osm.pbf",
            polygon_id="z:way/42",
            osm_version=1,
            website_text="copy",
            website_status="success",
            website_words=1,
            contact_text=None,
            contact_status="absent",
            contact_words=None,
        ),
        _row(
            osm_id=42,
            lat=40.7,
            lon=-74.0,
            source_pbf="a.osm.pbf",
            polygon_id="a:way/42",
            osm_version=1,
            website_text="copy",
            website_status="success",
            website_words=1,
            contact_text=None,
            contact_status="absent",
            contact_words=None,
        ),
    ]
    _write_run(first, rows, split=True)
    _write_run(second, list(reversed(rows)), split=False)

    assert compute_text_population_summary(first) == compute_text_population_summary(second)
    assert next(iter(iter_canonical_text_coordinates(first))).lon == -74.0
    assert next(iter(iter_canonical_text_coordinates(second))).lon == -74.0


def test_population_uses_regional_copies_for_a_canonical_run(tmp_path: Path) -> None:
    regional = tmp_path / "regional"
    canonical = tmp_path / "canonical"
    _write_run(
        regional,
        [
            _row(
                osm_id=7,
                lat=48.0,
                lon=2.0,
                source_pbf="a.osm.pbf",
                polygon_id="a:way/7",
                osm_version=1,
                website_text="regional",
                website_status="success",
                website_words=1,
                contact_text=None,
                contact_status="absent",
                contact_words=None,
            )
        ],
        split=False,
    )
    (regional / "analysis_observations").mkdir()
    (canonical / "analysis_observations").parent.mkdir(parents=True)
    (canonical / "analysis_observations").symlink_to(
        regional / "analysis_observations", target_is_directory=True
    )
    _write_run(
        canonical,
        [
            _row(
                osm_id=8,
                lat=1.0,
                lon=1.0,
                source_pbf="a.osm.pbf",
                polygon_id="a:way/8",
                osm_version=1,
                website_text="canonical only",
                website_status="success",
                website_words=2,
                contact_text=None,
                contact_status="absent",
                contact_words=None,
            )
        ],
        split=False,
    )

    population = compute_text_population_summary(canonical)

    assert population.unique_identity_count == 1
    assert population.website_total_words == 1
