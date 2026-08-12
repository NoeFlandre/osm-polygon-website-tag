"""Deterministic polygon-shard fixtures for schema migration tests."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA_V1_1

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
    "project_current_rows_to_legacy",
    "write_legacy_polygon_shard",
]
