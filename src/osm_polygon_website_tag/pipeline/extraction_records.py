"""Pure row construction for extraction output shards.

The public builders in this module turn copied scalar inputs and normalized
OSM tags into schema-shaped dictionaries. They perform no file, network,
SQLite, libosmium, or Parquet I/O. Shared tag projection remains in
``record_builders``; this module owns only output-specific fields, deterministic
JSON serialization, public-URL metadata, initial text state, and public-row
validation.

``pipeline.extraction`` retains aliases for its established private builder
names, while new code can use the explicit ``build_*_record`` boundary here.
"""

from __future__ import annotations

import datetime as dt
import json

from osm_polygon_website_tag.contracts.comparison_schema import (
    COMPARISON_OBSERVATION_SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.polygon_schema import SCHEMA_VERSION, validate_public_row
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA_VERSION
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields
from osm_polygon_website_tag.domain.tags import normalize_value
from osm_polygon_website_tag.domain.website import (
    classify_contact_website,
    classify_website,
    extract_contact_hostname,
    extract_hostname,
)
from osm_polygon_website_tag.pipeline.record_builders import (
    DerivedTags,
    derive_tags,
    derive_wikidata,
)


def build_public_record(
    *,
    polygon_id: str,
    source_pbf: str,
    region: str,
    tags_dict: dict[str, str],
    osm_type: str,
    osm_id: int,
    osm_version: int,
    osm_timestamp: dt.datetime,
    geom_text: str,
    centroid_text: str,
    centroid_kind: str,
    lat: float,
    lon: float,
    bbox: list[float],
    area_m2: float,
    area_bucket: str,
    derived: DerivedTags | None = None,
) -> dict[str, object]:
    """Build and validate one public polygon record."""
    if derived is None:
        derived = derive_tags(tags_dict)
    website_class, contact_class, website_hostname, contact_hostname = _website_metadata(derived)
    tags_json, tag_keys_json, bbox_json = _serialise_public_values(tags_dict, bbox)
    name_raw = normalize_value(tags_dict.get("name", "")) or None
    record: dict[str, object] = {
        "polygon_id": polygon_id,
        "region": region,
        "source_pbf": source_pbf,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": osm_version,
        "osm_timestamp": osm_timestamp,
        "name": name_raw,
        "website": derived.website,
        "contact_website": derived.contact_website,
        "has_website": derived.has_website,
        "has_contact_website": derived.has_contact_website,
        "has_any_website": derived.has_any_website,
        "website_class": website_class,
        "contact_website_class": contact_class,
        "website_hostname": website_hostname,
        "contact_website_hostname": contact_hostname,
        "tags": tags_json,
        "tag_keys": tag_keys_json,
        "tag_count": len(tags_dict),
        "osm_primary_tag": derived.primary_category,
        "geometry": geom_text,
        "centroid": centroid_text,
        "centroid_kind": centroid_kind,
        "lat": lat,
        "lon": lon,
        "bbox": bbox_json,
        "area_m2": area_m2,
        "area_bucket": area_bucket,
        "schema_version": SCHEMA_VERSION,
    }
    record.update(
        initial_text_fields(
            website_present=derived.has_website,
            contact_website_present=derived.has_contact_website,
        )
    )
    validate_public_row(record)
    return record


def _website_metadata(
    derived: DerivedTags,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Derive public URL classes and hostnames from one shared projection."""
    website_class = classify_website(derived.website).value if derived.website else None
    contact_class = (
        classify_contact_website(derived.contact_website).value if derived.contact_website else None
    )
    website_hostname = extract_hostname(derived.website) if derived.website else None
    contact_hostname = (
        extract_contact_hostname(derived.contact_website) if derived.contact_website else None
    )
    return website_class, contact_class, website_hostname, contact_hostname


def _serialise_public_values(tags_dict: dict[str, str], bbox: list[float]) -> tuple[str, str, str]:
    """Serialise tag and bounding-box values with stable separators."""
    tag_keys_sorted = sorted(tags_dict.keys())
    return (
        json.dumps(tags_dict, sort_keys=True, separators=(",", ":")),
        json.dumps(tag_keys_sorted, separators=(",", ":")),
        json.dumps(bbox, separators=(",", ":")),
    )


def build_comparison_record(
    *,
    source_pbf: str,
    region: str,
    tags_dict: dict[str, str],
    osm_type: str,
    osm_id: int,
    osm_version: int,
    osm_timestamp: dt.datetime,
    derived: DerivedTags | None = None,
) -> dict[str, object]:
    """Build one compact website/Wikidata comparison observation."""
    if derived is None:
        derived = derive_tags(tags_dict)
    wikidata, has_wikidata = derive_wikidata(tags_dict)
    return {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": osm_version,
        "osm_timestamp": osm_timestamp,
        "source_pbf": source_pbf,
        "region": region,
        "primary_category": derived.primary_category,
        "website": derived.website,
        "contact_website": derived.contact_website,
        "wikidata": wikidata,
        "has_website": derived.has_website,
        "has_contact_website": derived.has_contact_website,
        "has_any_website": derived.has_any_website,
        "has_wikidata": has_wikidata,
        "schema_version": COMPARISON_OBSERVATION_SCHEMA_VERSION,
    }


def build_rejection_record(
    *,
    source_pbf: str,
    region: str,
    tags_dict: dict[str, str],
    osm_type: str,
    osm_id: int,
    osm_version: int,
    osm_timestamp: dt.datetime,
    candidate_kind: str,
    rejection_kind: str,
    message: str,
    derived: DerivedTags | None = None,
) -> dict[str, object]:
    """Build one expected extraction-rejection record."""
    if derived is None:
        derived = derive_tags(tags_dict)
    wikidata, has_wikidata = derive_wikidata(tags_dict)
    return {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": osm_version,
        "osm_timestamp": osm_timestamp,
        "source_pbf": source_pbf,
        "region": region,
        "primary_category": derived.primary_category,
        "website": derived.website,
        "contact_website": derived.contact_website,
        "wikidata": wikidata,
        "has_website": derived.has_website,
        "has_contact_website": derived.has_contact_website,
        "has_any_website": derived.has_any_website,
        "has_wikidata": has_wikidata,
        "candidate_kind": candidate_kind,
        "rejection_kind": rejection_kind,
        "message": message,
        "schema_version": REJECTION_SCHEMA_VERSION,
    }


__all__ = [
    "build_comparison_record",
    "build_public_record",
    "build_rejection_record",
]
