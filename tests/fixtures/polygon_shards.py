"""Deterministic polygon-shard fixtures for schema migration tests."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    POLYGON_PUBLIC_SCHEMA_V1_3,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
)
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields

LegacySchemaVersion = Literal["v1.1", "v1.2"]
PolygonSchemaVersion = Literal["v1.1", "v1.2", "v1.3", "v1.4", "v1.5"]

_POLYGON_SCHEMAS = {
    "v1.1": POLYGON_PUBLIC_SCHEMA_V1_1,
    "v1.2": POLYGON_PUBLIC_SCHEMA_V1_2,
    "v1.3": POLYGON_PUBLIC_SCHEMA_V1_3,
    "v1.4": POLYGON_PUBLIC_SCHEMA_V1_4,
    "v1.5": POLYGON_PUBLIC_SCHEMA_V1_5,
}


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


def polygon_row(
    schema_version: PolygonSchemaVersion = "v1.3", **overrides: object
) -> dict[str, object]:
    """Return a representative row matching one public polygon schema.

    Schema-specific additions are filled from the Arrow field types and then
    given useful stage defaults. ``overrides`` may replace known fields only.
    """
    if schema_version == "v1.1":
        row = legacy_polygon_row()
    elif schema_version == "v1.2":
        row = v1_2_polygon_row()
    else:
        row = polygon_row_v1_3()

    schema = _POLYGON_SCHEMAS[schema_version]
    for field in schema:
        row.setdefault(field.name, _default_for_field(field))
    row["schema_version"] = schema_version

    if schema_version in {"v1.4", "v1.5"}:
        has_website = overrides.get("website", row.get("website")) is not None
        has_contact_website = (
            overrides.get("contact_website", row.get("contact_website")) is not None
        )
        website_text = "example text." if has_website else None
        contact_text = "contact example text." if has_contact_website else None
        stage_defaults = {
            "website_text": website_text,
            "website_word_count": 2 if has_website else None,
            "website_text_status": "success" if has_website else "absent",
            "contact_website_text": contact_text,
            "contact_website_word_count": 3 if has_contact_website else None,
            "contact_website_text_status": "success" if has_contact_website else "absent",
            "website_language": "eng_Latn" if has_website else None,
            "website_language_probability": 0.99 if has_website else None,
            "contact_website_language": "eng_Latn" if has_contact_website else None,
            "contact_website_language_probability": 0.99 if has_contact_website else None,
            "website_sentences": ["example text."] if has_website else None,
            "website_sentence_count": 1 if has_website else None,
            "website_sentence_status": "success" if has_website else "absent",
            "contact_website_sentences": ["contact example text."] if has_contact_website else None,
            "contact_website_sentence_count": 1 if has_contact_website else None,
            "contact_website_sentence_status": "success" if has_contact_website else "absent",
        }
        row.update({name: value for name, value in stage_defaults.items() if name in row})

    cleared_urls = False
    for url_field, flag_field, class_field, hostname_field in (
        ("website", "has_website", "website_class", "website_hostname"),
        (
            "contact_website",
            "has_contact_website",
            "contact_website_class",
            "contact_website_hostname",
        ),
    ):
        if url_field in overrides and overrides[url_field] is None:
            cleared_urls = True
            row[url_field] = None
            row[flag_field] = False
            row[class_field] = None
            row[hostname_field] = None
    if cleared_urls:
        has_website = overrides.get("has_website", row["has_website"])
        has_contact_website = overrides.get("has_contact_website", row["has_contact_website"])
        row["has_any_website"] = bool(has_website or has_contact_website)
    if "preferred_website" in row and ("website" in overrides or "contact_website" in overrides):
        website = overrides.get("website", row["website"])
        contact_website = overrides.get("contact_website", row["contact_website"])
        row["preferred_website"] = website or contact_website
        row["preferred_website_source"] = "website" if website else "contact:website"

    return _apply_overrides(row, overrides)


def language_polygon_row(
    index: int,
    *,
    language: str | None = "eng_Latn",
    text: str = "One|Two",
) -> dict[str, object]:
    """Return one v1.4 row consumed by language and sentence pipeline tests."""
    return polygon_row(
        "v1.4",
        contact_website=None,
        has_contact_website=False,
        contact_website_class=None,
        contact_website_hostname=None,
        polygon_id=f"source:way/{index}",
        website_text=text,
        website_word_count=len(text.split()),
        website_text_status="success",
        website_language=language,
        website_language_probability=0.99 if language is not None else None,
    )


def partition_aggregate_row(
    *,
    polygon_id: str,
    source_pbf: str,
    region: str = "monaco",
    osm_type: str = "way",
    website: str = "https://example.com",
    website_class: str = "absolute_url",
    website_hostname: str | None = "example.com",
    wikidata: str | None = "Q42",
    osm_primary_tag: str = "building",
    area_bucket: str = "10-100m2",
) -> dict[str, object]:
    """Return a v1.3 row with the dimensions used by aggregation tests."""
    return polygon_row(
        "v1.3",
        polygon_id=polygon_id,
        region=region,
        source_pbf=source_pbf,
        osm_type=osm_type,
        osm_id=100,
        osm_version=1,
        website=website,
        website_class=website_class,
        website_hostname=website_hostname,
        name=None,
        tags=json.dumps({"wikidata": wikidata} if wikidata else {}),
        tag_keys="[]",
        tag_count=0,
        osm_primary_tag=osm_primary_tag,
        area_m2=0.0,
        area_bucket=area_bucket,
    )


def deduplicate_polygon_row(
    *,
    source_pbf: str,
    osm_id: int,
    osm_version: int,
    website: str,
    timestamp_day: int,
    contact: str | None = None,
) -> dict[str, object]:
    """Return one v1.3 row with deterministic identity and revision fields."""
    stem = source_pbf.removesuffix(".osm.pbf")
    return polygon_row_v1_3(
        polygon_id=f"{stem}:way/{osm_id}",
        website=website,
        contact=contact,
        source_pbf=source_pbf,
        osm_id=osm_id,
        osm_version=osm_version,
        osm_timestamp=dt.datetime(2026, 1, timestamp_day, tzinfo=dt.UTC),
        website_text=f"text from {website}",
        website_word_count=3,
        website_text_status="success",
    )


def text_population_polygon_row(
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
    """Return a v1.4 row for global website-text population tests."""
    return polygon_row(
        "v1.4",
        osm_type="way",
        osm_id=osm_id,
        osm_version=osm_version,
        osm_timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        source_pbf=source_pbf,
        polygon_id=polygon_id,
        lat=lat,
        lon=lon,
        website="https://example.org" if website_text else None,
        has_website=bool(website_text),
        website_class="absolute_url" if website_text else None,
        website_hostname="example.org" if website_text else None,
        contact_website="https://contact.example.org" if contact_text else None,
        has_contact_website=bool(contact_text),
        contact_website_class="absolute_url" if contact_text else None,
        contact_website_hostname="contact.example.org" if contact_text else None,
        has_any_website=bool(website_text or contact_text),
        website_text=website_text,
        website_text_status=website_status,
        website_word_count=website_words,
        contact_website_text=contact_text,
        contact_website_text_status=contact_status,
        contact_website_word_count=contact_words,
        website_language="eng" if website_text else None,
        contact_website_language="fra" if contact_text else None,
        website_language_probability=0.99 if website_text else None,
        contact_website_language_probability=0.99 if contact_text else None,
    )


def _default_for_field(field: pa.Field) -> object:
    """Return a type-correct empty value for a schema-generated row field."""
    if field.nullable:
        return None
    if pa.types.is_boolean(field.type):
        return False
    if pa.types.is_integer(field.type):
        return 0
    if pa.types.is_floating(field.type):
        return 0.0
    if pa.types.is_timestamp(field.type):
        return pa.scalar(0, type=field.type).as_py()
    if pa.types.is_list(field.type):
        return []
    if pa.types.is_struct(field.type):
        return {}
    return ""


def sentence_input_row(**overrides: object) -> dict[str, object]:
    """Return the text/language fields consumed by the sentence stage."""
    row: dict[str, object] = {
        "polygon_id": "source:way/1",
        "website_text_status": "success",
        "website_text": "One. Two.",
        "website_language": "eng_Latn",
        "contact_website_text_status": "absent",
        "contact_website_text": None,
        "contact_website_language": None,
    }
    return _apply_overrides(row, overrides)


def sentence_verification_row(**overrides: object) -> dict[str, object]:
    """Return one consistent v1.5 sentence-verification row."""
    row: dict[str, object] = {
        "website_text_status": "success",
        "website_language": "eng_Latn",
        "website_sentences": ["One. ", "Two."],
        "website_sentence_count": 2,
        "website_sentence_status": "success",
        "contact_website_text_status": "absent",
        "contact_website_language": None,
        "contact_website_sentences": None,
        "contact_website_sentence_count": None,
        "contact_website_sentence_status": "absent",
    }
    return _apply_overrides(row, overrides)


def text_verification_row(
    *,
    website: str | None = "https://example.org",
    text: str | None = "one two",
    word_count: int | None = 2,
    status: str | None = "success",
) -> dict[str, object]:
    """Return the paired website/contact fields used by text verification."""
    return {
        "website": website,
        "website_text": text,
        "website_word_count": word_count,
        "website_text_status": status,
        "contact_website": None,
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


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
    "PolygonSchemaVersion",
    "legacy_polygon_row",
    "polygon_row",
    "polygon_row_v1_3",
    "project_current_rows_to_legacy",
    "sentence_input_row",
    "sentence_verification_row",
    "text_verification_row",
    "v1_2_polygon_row",
    "write_legacy_polygon_shard",
]
