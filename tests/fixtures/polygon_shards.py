"""Deterministic polygon-shard fixtures for schema migration tests."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA_V1_1
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields

LegacySchemaVersion = Literal["v1.1", "v1.2"]


def legacy_polygon_row(
    *,
    polygon_id: str = "source:way/1",
    website: str | None = "https://example.org",
    contact: str | None = "https://contact.example.org",
) -> dict[str, object]:
    """Return one representative v1.1 public polygon row."""
    return {
        "polygon_id": polygon_id,
        "region": "source",
        "source_pbf": "source.osm.pbf",
        "osm_type": "way",
        "osm_id": int(polygon_id.rsplit("/", 1)[1]),
        "osm_version": 1,
        "osm_timestamp": pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py(),
        "name": None,
        "website": website,
        "contact_website": contact,
        "has_website": website is not None,
        "has_contact_website": contact is not None,
        "has_any_website": True,
        "website_class": "absolute_url" if website else None,
        "contact_website_class": "absolute_url" if contact else None,
        "website_hostname": "example.org" if website else None,
        "contact_website_hostname": "contact.example.org" if contact else None,
        "preferred_website": website or contact,
        "preferred_website_source": "website" if website else "contact:website",
        "wikidata": None,
        "wikidata_qid": None,
        "wikidata_class": None,
        "tags": json.dumps({"website": website, "contact:website": contact}),
        "tag_keys": '["contact:website","website"]',
        "tag_count": 2,
        "osm_primary_tag": "building",
        "geometry": '{"type":"Polygon","coordinates":[]}',
        "centroid": '{"type":"Point","coordinates":[0,0]}',
        "centroid_kind": "lambert_azimuthal_equal_area",
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0,0,0,0]",
        "area_m2": 1.0,
        "area_km2": 0.000001,
        "area_bucket": "<10m2",
        "schema_version": "v1.1",
    }


_V1_3_REMOVED_FIELDS = (
    "preferred_website",
    "preferred_website_source",
    "wikidata",
    "wikidata_qid",
    "wikidata_class",
    "area_km2",
)


def v1_2_polygon_row(**overrides: object) -> dict[str, object]:
    """Return one v1.2 public polygon row with a successful website text.

    ``overrides`` replace existing fields only, so a misspelled field fails.
    """
    row: dict[str, object] = {
        "polygon_id": "p1",
        "region": "monaco",
        "source_pbf": "monaco-latest.osm.pbf",
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py(),
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
    return _apply_overrides(row, overrides)


def polygon_row_v1_3(
    *,
    polygon_id: str = "source:way/1",
    website: str | None = "https://example.org",
    contact: str | None = "https://contact.example.org",
    **overrides: object,
) -> dict[str, object]:
    """Return a v1.1 fixture row migrated to v1.3 with initial text fields.

    ``overrides`` replace existing fields only, so a misspelled field fails.
    """
    row = legacy_polygon_row(polygon_id=polygon_id, website=website, contact=contact)
    for field in _V1_3_REMOVED_FIELDS:
        row.pop(field)
    row.update(
        initial_text_fields(
            website_present=website is not None,
            contact_website_present=contact is not None,
        )
    )
    row["schema_version"] = "v1.3"
    return _apply_overrides(row, overrides)


def _apply_overrides(row: dict[str, object], overrides: Mapping[str, object]) -> dict[str, object]:
    """Replace known fields, rejecting names the row does not carry."""
    unknown = sorted(set(overrides) - set(row))
    if unknown:
        raise KeyError(f"unknown polygon row fields: {unknown}")
    row.update(overrides)
    return row


def write_legacy_polygon_shard(path: Path, rows: list[dict[str, object]]) -> None:
    """Write representative rows using the v1.1 public schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA_V1_1), path)


def project_current_rows_to_legacy(
    rows: Sequence[Mapping[str, object]],
    *,
    schema_version: LegacySchemaVersion,
) -> list[dict[str, object]]:
    """Project current rows back to a pre-v1.3 schema for migration tests."""
    projected: list[dict[str, object]] = []
    for original in rows:
        row = dict(original)
        website = row.get("website")
        contact = row.get("contact_website")
        row.update(
            {
                "preferred_website": website or contact,
                "preferred_website_source": "website" if website else "contact:website",
                "wikidata": None,
                "wikidata_qid": None,
                "wikidata_class": None,
                "area_km2": cast(float, row["area_m2"]) / 1_000_000,
                "schema_version": schema_version,
            }
        )
        projected.append(row)
    return projected


__all__ = [
    "LegacySchemaVersion",
    "legacy_polygon_row",
    "polygon_row_v1_3",
    "project_current_rows_to_legacy",
    "v1_2_polygon_row",
    "write_legacy_polygon_shard",
]
