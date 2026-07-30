"""Comparison observation schema (v1.1).

The comparison-observation schema is the contract for every Parquet
file in the published ``analysis_observations/`` directory. A row
exists for every polygon that qualifies for inclusion in either the
public dataset OR the Wikidata-only comparison set:

    has_any_website OR has_wikidata

The schema records all three boolean dimensions required for the
eight-cell contingency cube (W, C, D) and the original trimmed
values, never the derived fields. Eight-cell counts and all headline
metrics are SQL-derived from these rows.
"""

from __future__ import annotations

import pyarrow as pa

COMPARISON_OBSERVATION_SCHEMA_VERSION = "v1.1"


COMPARISON_OBSERVATION_SCHEMA: pa.Schema = pa.schema(
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
        pa.field("schema_version", pa.string(), nullable=False),
    ]
)


_COLUMN_DOCS: dict[str, str] = {
    "osm_type": '``"way"`` for a closed polygonal way, ``"relation"`` for a multipolygon/boundary relation.',
    "osm_id": "Original OSM numeric identifier of the way or relation.",
    "osm_version": "OSM version number as recorded on the object.",
    "osm_timestamp": "OSM last-edit timestamp, UTC.",
    "source_pbf": "Original source PBF filename.",
    "region": "Region label derived from the source PBF filename.",
    "primary_category": "Primary OSM category key (see categories.CATEGORY_ORDER).",
    "website": "Trimmed original ``website`` value; ``None`` when absent or whitespace-only.",
    "contact_website": "Trimmed original ``contact:website`` value; ``None`` when absent or whitespace-only.",
    "wikidata": "Trimmed original ``wikidata`` value; ``None`` when absent. Malformed-but-non-empty values are retained verbatim.",
    "has_website": "``True`` iff the trimmed ``website`` tag is non-empty.",
    "has_contact_website": "``True`` iff the trimmed ``contact:website`` tag is non-empty.",
    "has_any_website": "``True`` iff at least one of ``website`` or ``contact:website`` is non-empty.",
    "has_wikidata": "``True`` iff the trimmed ``wikidata`` tag is non-empty.",
    "schema_version": "Schema version of the comparison-observation table.",
}


def comparison_column_names(schema: pa.Schema) -> list[str]:
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
    "COMPARISON_OBSERVATION_SCHEMA",
    "COMPARISON_OBSERVATION_SCHEMA_VERSION",
    "column_doc",
    "column_documentation",
    "comparison_column_names",
]
