"""Global website-text population contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.reporting import text_population
from osm_polygon_website_tag.reporting.artifact_inventory import data_manifest_sha256
from osm_polygon_website_tag.reporting.text_population import (
    TextCoordinate,
    compute_text_population_summary,
    iter_canonical_text_coordinates,
    text_population_manifest_entries,
)
from osm_polygon_website_tag.reporting.verification.language import verify_language_paths
from osm_polygon_website_tag.reporting.verification.text import verify_text_paths


def _row(
    *,
    osm_id: int,
    lat: float,
    lon: float,
    source_pbf: str,
    polygon_id: str,
    osm_version: int,
    website_text: str | None,
    website_status: str | None,
    website_words: int | None,
    contact_text: str | None,
    contact_status: str | None,
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


def test_population_breaks_payload_ties_without_using_row_order(tmp_path: Path) -> None:
    first = _row(
        osm_id=42,
        lat=48.85,
        lon=2.35,
        source_pbf="same.osm.pbf",
        polygon_id="same:way/42",
        osm_version=1,
        website_text="same copy",
        website_status="success",
        website_words=1,
        contact_text=None,
        contact_status="absent",
        contact_words=None,
    )
    first["website"] = "https://z.example.org"
    second = dict(first)
    second["website"] = "https://a.example.org"
    second["website_word_count"] = 2
    first_run = tmp_path / "first"
    second_run = tmp_path / "second"
    _write_run(first_run, [first, second], split=False)
    _write_run(second_run, [second, first], split=False)

    assert compute_text_population_summary(first_run).website_total_words == 2
    assert compute_text_population_summary(second_run).website_total_words == 2


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
    entries = text_population_manifest_entries(canonical)
    assert [entry["path"] for entry in entries] == ["regional/polygons/a.parquet"]
    assert [entry["remote_path"] for entry in entries] == ["polygons/a.parquet"]
    assert entries[0]["size_bytes"] == (regional / "polygons" / "a.parquet").stat().st_size

    before = data_manifest_sha256(canonical)
    regional_path = regional / "polygons" / "a.parquet"
    regional_table = pq.read_table(regional_path)
    regional_table = regional_table.set_column(
        regional_table.schema.get_field_index("website_text"),
        "website_text",
        pa.array(["changed regional input"]),
    )
    pq.write_table(regional_table, regional_path)

    assert data_manifest_sha256(canonical) != before


def test_release_text_validation_rejects_invalid_word_counts(tmp_path: Path) -> None:
    path = tmp_path / "source.parquet"
    row = _row(
        osm_id=7,
        lat=48.0,
        lon=2.0,
        source_pbf="a.osm.pbf",
        polygon_id="a:way/7",
        osm_version=1,
        website_text="one two",
        website_status="success",
        website_words=99,
        contact_text=None,
        contact_status="absent",
        contact_words=None,
    )
    pq.write_table(pa.Table.from_pylist([row]), path)
    errors: list[str] = []

    verify_text_paths([path], "complete", errors)

    assert any("word count does not match" in error for error in errors)


def test_release_language_validation_rejects_incomplete_language_pairs(tmp_path: Path) -> None:
    path = tmp_path / "source.parquet"
    pq.write_table(
        pa.table(
            {
                "website_text_status": ["success"],
                "website_language": ["eng_Latn"],
                "website_language_probability": [None],
                "contact_website_text_status": ["absent"],
                "contact_website_language": [None],
                "contact_website_language_probability": [None],
            }
        ),
        path,
    )
    errors: list[str] = []

    verify_language_paths([path], errors)

    assert any("language probability is invalid" in error for error in errors)


def test_population_counts_null_status_as_a_failure_identity(tmp_path: Path) -> None:
    row = _row(
        osm_id=9,
        lat=48.0,
        lon=2.0,
        source_pbf="a.osm.pbf",
        polygon_id="a:way/9",
        osm_version=1,
        website_text=None,
        website_status=None,
        website_words=None,
        contact_text=None,
        contact_status="absent",
        contact_words=None,
    )
    _write_run(tmp_path, [row], split=False)

    population = compute_text_population_summary(tmp_path)

    assert population.unique_identity_count == 0
    assert population.website_failure_identity_count == 1


def test_population_ignores_unicode_whitespace_only_text(tmp_path: Path) -> None:
    row = _row(
        osm_id=10,
        lat=48.0,
        lon=2.0,
        source_pbf="a.osm.pbf",
        polygon_id="a:way/10",
        osm_version=1,
        website_text="\u00a0\u2003\u202f",
        website_status="success",
        website_words=0,
        contact_text=None,
        contact_status="absent",
        contact_words=None,
    )
    _write_run(tmp_path, [row], split=False)

    population = compute_text_population_summary(tmp_path)

    assert population.unique_identity_count == 0
    assert population.website_identity_count == 0


def test_population_status_buckets_use_failure_precedence(tmp_path: Path) -> None:
    rows = [
        _row(
            osm_id=11,
            lat=48.0,
            lon=2.0,
            source_pbf="a.osm.pbf",
            polygon_id="a:way/11",
            osm_version=1,
            website_text=None,
            website_status="empty",
            website_words=None,
            contact_text=None,
            contact_status="absent",
            contact_words=None,
        ),
        _row(
            osm_id=11,
            lat=48.0,
            lon=2.0,
            source_pbf="b.osm.pbf",
            polygon_id="b:way/11",
            osm_version=1,
            website_text=None,
            website_status="fetch_error",
            website_words=None,
            contact_text=None,
            contact_status="absent",
            contact_words=None,
        ),
    ]
    _write_run(tmp_path, rows, split=False)

    population = compute_text_population_summary(tmp_path)

    assert population.website_empty_identity_count == 0
    assert population.website_failure_identity_count == 1


def test_population_breaks_a_prefix_tie_on_extracted_text(tmp_path: Path) -> None:
    """Two rows identical up to their text must still pick the text-ordered winner."""
    shared = {
        "osm_id": 42,
        "lat": 48.0,
        "lon": 2.0,
        "source_pbf": "a.osm.pbf",
        "polygon_id": "a:way/42",
        "osm_version": 1,
        "website_status": "success",
        "contact_text": None,
        "contact_status": "absent",
        "contact_words": None,
    }
    rows = [
        _row(website_text="zulu", website_words=9, **shared),
        _row(website_text="alpha", website_words=4, **shared),
    ]
    _write_run(tmp_path, rows, split=False)

    population = compute_text_population_summary(tmp_path)

    assert population.unique_identity_count == 1
    assert population.website_identity_count == 1
    assert population.website_total_words == 4


def test_a_prefix_tie_restores_the_text_bearing_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The text tie-break path must be the one that resolves a prefix tie."""
    restored: list[bool] = []
    original = text_population._restore_text_population_view

    def record(connection: object) -> None:
        restored.append(True)
        original(connection)

    monkeypatch.setattr(text_population, "_restore_text_population_view", record)
    shared = {
        "osm_id": 42,
        "lat": 48.0,
        "lon": 2.0,
        "source_pbf": "a.osm.pbf",
        "polygon_id": "a:way/42",
        "osm_version": 1,
        "website_status": "success",
        "contact_text": None,
        "contact_status": "absent",
        "contact_words": None,
    }
    _write_run(
        tmp_path,
        [
            _row(website_text="zulu", website_words=9, **shared),
            _row(website_text="alpha", website_words=4, **shared),
        ],
        split=False,
    )

    assert compute_text_population_summary(tmp_path).website_total_words == 4
    assert restored == [True]
