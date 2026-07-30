"""Rejection schema (v1.1).

A rejection row records an OSM polygon candidate (closed way or
relation of a supported polygon type) that did NOT become either a
public polygon row or a comparison-observation row, plus the reason.
Rejections are excluded from the eight-cell cube and from the public
counts but contribute to a separate ``rejection_count`` denominator.

Rejections are stored in their own directory ``rejections/`` so the
``analysis_observations/*.parquet`` glob never matches them.
"""

from __future__ import annotations

import pyarrow as pa

REJECTION_SCHEMA_VERSION = "v1.1"


REJECTION_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("osm_type", pa.string(), nullable=False),
        pa.field("osm_id", pa.int64(), nullable=False),
        pa.field("osm_version", pa.int32(), nullable=False),
        pa.field("osm_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source_pbf", pa.string(), nullable=False),
        pa.field("region", pa.string(), nullable=False),
        pa.field("primary_category", pa.string(), nullable=False),
        pa.field("website", pa.string(), nullable=True),
        pa.field("contact_website", pa.string(), nullable=True),
        pa.field("wikidata", pa.string(), nullable=True),
        pa.field("has_website", pa.bool_(), nullable=False),
        pa.field("has_contact_website", pa.bool_(), nullable=False),
        pa.field("has_any_website", pa.bool_(), nullable=False),
        pa.field("has_wikidata", pa.bool_(), nullable=False),
        pa.field("candidate_kind", pa.string(), nullable=False),
        pa.field("rejection_kind", pa.string(), nullable=False),
        pa.field("message", pa.string(), nullable=False),
        pa.field("schema_version", pa.string(), nullable=False),
    ]
)


_COLUMN_DOCS: dict[str, str] = {
    "osm_type": '``"way"`` or ``"relation"`` -- the OSM object that was rejected.',
    "osm_id": "Original OSM numeric identifier.",
    "osm_version": "OSM version number as recorded on the object.",
    "osm_timestamp": "OSM last-edit timestamp, UTC.",
    "source_pbf": "Original source PBF filename.",
    "region": "Region label derived from the source PBF filename.",
    "primary_category": "Primary OSM category key (see categories.CATEGORY_ORDER).",
    "website": "Trimmed original ``website`` value (may be null).",
    "contact_website": "Trimmed original ``contact:website`` value (may be null).",
    "wikidata": "Trimmed original ``wikidata`` value (may be null).",
    "has_website": "``True`` iff the trimmed ``website`` tag is non-empty.",
    "has_contact_website": "``True`` iff the trimmed ``contact:website`` tag is non-empty.",
    "has_any_website": "``True`` iff at least one website key is non-empty.",
    "has_wikidata": "``True`` iff the trimmed ``wikidata`` tag is non-empty.",
    "candidate_kind": (
        "Why this OSM object was considered a candidate: ``closed_way`` or ``relation_polygon``."
    ),
    "rejection_kind": (
        "Short identifier of the rejection reason. Examples: "
        "``no_area_callback``, ``antimeridian``, ``non_finite_area``, "
        "``duplicate_area_callback``, ``open_way_with_website``."
    ),
    "message": "Human-readable message describing the rejection.",
    "schema_version": "Schema version of the rejection table.",
}


def rejection_column_names(schema: pa.Schema) -> list[str]:
    """Return ``schema``'s column names in declaration order."""
    return [field.name for field in schema]


def column_doc(name: str) -> str:
    """Return the documentation string for column ``name``."""
    if name not in _COLUMN_DOCS:
        raise KeyError(f"no documentation for column {name!r}")
    return _COLUMN_DOCS[name]


def column_documentation() -> dict[str, str]:
    """Return a copy of the column documentation table for card rendering."""
    return dict(_COLUMN_DOCS)


__all__ = [
    "REJECTION_SCHEMA",
    "REJECTION_SCHEMA_VERSION",
    "column_doc",
    "column_documentation",
    "rejection_column_names",
]
