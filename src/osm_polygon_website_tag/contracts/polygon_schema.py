"""Public polygon schema and column documentation.

Schema version: ``v1.3``.

The schema is the contract for every Parquet file in the published
``polygons/`` directory. Every published row must satisfy the row-level
invariants documented in :func:`column_doc` and enforced by
:func:`validate_public_row`.

Inclusion rule
--------------

Every public row satisfies:

    has_any_website == True
    AND (website != "" OR contact_website != "")

This invariant is enforced row-by-row by :func:`validate_public_row`
before the row is written to the public shard. Violations raise
:class:`PublicRowInvariantError`.

Nullability
-----------

The individual website values, classes, hostnames, and name remain
nullable. Every public row carries at least one non-empty website value.

Schema versioning
-----------------

The schema version is part of the dataset contract. A new schema MUST be
introduced with a new ``SCHEMA_VERSION`` (and a new column order) rather
than a silent in-place change. The verifier rejects schema drift.

The exact metadata-aware comparison is centralized in
:func:`schema_matches`; :func:`is_supported_public_polygon_schema` is the
compatibility predicate for the three public versions accepted during
resumption and migration.
"""

from __future__ import annotations

import pyarrow as pa

from osm_polygon_website_tag.contracts.text_schema import TEXT_FIELDS

SCHEMA_VERSION = "v1.3"


PUBLIC_ROW_INVARIANT_ERROR = "PublicRowInvariantError"


class PublicRowInvariantError(ValueError):
    """Raised when a row violates a public-shard invariant."""


# Public polygon schema. The order here is the order in every Parquet
# file written by this project.
POLYGON_PUBLIC_SCHEMA_V1_1: pa.Schema = pa.schema(
    [
        pa.field("polygon_id", pa.string(), nullable=False),
        pa.field("region", pa.string(), nullable=False),
        pa.field("source_pbf", pa.string(), nullable=False),
        pa.field("osm_type", pa.string(), nullable=False),
        pa.field("osm_id", pa.int64(), nullable=False),
        pa.field("osm_version", pa.int32(), nullable=False),
        pa.field("osm_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("name", pa.string(), nullable=True),
        pa.field("website", pa.string(), nullable=True),
        pa.field("contact_website", pa.string(), nullable=True),
        pa.field("has_website", pa.bool_(), nullable=False),
        pa.field("has_contact_website", pa.bool_(), nullable=False),
        pa.field("has_any_website", pa.bool_(), nullable=False),
        pa.field("website_class", pa.string(), nullable=True),
        pa.field("contact_website_class", pa.string(), nullable=True),
        pa.field("website_hostname", pa.string(), nullable=True),
        pa.field("contact_website_hostname", pa.string(), nullable=True),
        pa.field("preferred_website", pa.string(), nullable=False),
        pa.field("preferred_website_source", pa.string(), nullable=False),
        pa.field("wikidata", pa.string(), nullable=True),
        pa.field("wikidata_qid", pa.string(), nullable=True),
        pa.field("wikidata_class", pa.string(), nullable=True),
        pa.field("tags", pa.string(), nullable=False),
        pa.field("tag_keys", pa.string(), nullable=False),
        pa.field("tag_count", pa.int32(), nullable=False),
        pa.field("osm_primary_tag", pa.string(), nullable=False),
        pa.field("geometry", pa.string(), nullable=False),
        pa.field("centroid", pa.string(), nullable=False),
        pa.field("centroid_kind", pa.string(), nullable=False),
        pa.field("lat", pa.float64(), nullable=False),
        pa.field("lon", pa.float64(), nullable=False),
        pa.field("bbox", pa.string(), nullable=False),
        pa.field("area_m2", pa.float64(), nullable=False),
        pa.field("area_km2", pa.float64(), nullable=False),
        pa.field("area_bucket", pa.string(), nullable=False),
        pa.field("schema_version", pa.string(), nullable=False),
    ]
)

POLYGON_PUBLIC_SCHEMA_V1_2: pa.Schema = pa.schema([*POLYGON_PUBLIC_SCHEMA_V1_1, *TEXT_FIELDS])

_REMOVED_V1_3_FIELDS = frozenset(
    {
        "preferred_website",
        "preferred_website_source",
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "area_km2",
    }
)

POLYGON_PUBLIC_SCHEMA: pa.Schema = pa.schema(
    field for field in POLYGON_PUBLIC_SCHEMA_V1_2 if field.name not in _REMOVED_V1_3_FIELDS
)

_SUPPORTED_PUBLIC_POLYGON_SCHEMAS: tuple[pa.Schema, ...] = (
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    POLYGON_PUBLIC_SCHEMA,
)


def schema_matches(actual: pa.Schema, expected: pa.Schema) -> bool:
    """Return whether two Arrow schemas match, including metadata."""
    return actual.equals(expected, check_metadata=True)


def is_supported_public_polygon_schema(schema: pa.Schema) -> bool:
    """Return whether ``schema`` is one of the supported public versions."""
    return any(schema_matches(schema, candidate) for candidate in _SUPPORTED_PUBLIC_POLYGON_SCHEMAS)


_COLUMN_DOCS: dict[str, str] = {
    "polygon_id": (
        "Deterministic source-scoped identifier of the form ``<source-stem>:<osm_type>/<osm_id>``."
    ),
    "region": (
        "Human-readable region label derived from the source PBF filename "
        "(Geofabrik convention). Stable and independent of the source "
        "filename's extension."
    ),
    "source_pbf": (
        "Original source PBF filename (no path). "
        "Reconstructed from the run's processed-pbfs manifest."
    ),
    "osm_type": (
        '``"way"`` for a closed polygonal way, ``"relation"`` for an '
        "assembled multipolygon or boundary relation."
    ),
    "osm_id": (
        "Original OSM numeric identifier of the way or relation. "
        "Way and relation namespaces are kept distinct."
    ),
    "osm_version": ("OSM version number as recorded on the object at extraction time."),
    "osm_timestamp": ("OSM last-edit timestamp as recorded on the object at extraction time. UTC."),
    "name": ("Trimmed ``name`` tag value. ``None`` when the tag is absent."),
    "website": (
        "Trimmed original ``website`` tag value. Nullable: ``None`` when "
        "the tag is absent or whitespace-only."
    ),
    "contact_website": (
        "Trimmed original ``contact:website`` tag value. Nullable: "
        "``None`` when the tag is absent or whitespace-only."
    ),
    "has_website": ("``True`` iff the trimmed ``website`` tag is non-empty."),
    "has_contact_website": (
        "``True`` iff the trimmed ``contact:website`` tag is non-empty. "
        "Only the exact key is checked; unrelated ``contact:*`` keys do "
        "not contribute."
    ),
    "has_any_website": (
        "``True`` iff ``has_website`` OR ``has_contact_website``. Always "
        "``True`` in the public shard (inclusion invariant)."
    ),
    "website_class": (
        "Discrete classification of the ``website`` value, or ``None`` when the tag is absent."
    ),
    "contact_website_class": (
        "Discrete classification of the ``contact:website`` value, or "
        "``None`` when the tag is absent."
    ),
    "website_hostname": (
        "Lowercased hostname extracted from ``website``, or ``None`` when "
        "the value is not parseable as a URL or bare hostname."
    ),
    "contact_website_hostname": (
        "Lowercased hostname extracted from ``contact_website``, or "
        "``None`` when the value is not parseable."
    ),
    "tags": (
        "Deterministic JSON object containing every tag of the source OSM "
        "object, with keys sorted. ``{}`` when the object carries no tags."
    ),
    "tag_keys": (
        "Deterministic JSON array of every tag key of the source OSM "
        "object, sorted lexicographically."
    ),
    "tag_count": ("Number of tags carried by the source OSM object. Always non-negative."),
    "osm_primary_tag": (
        "Primary OSM category key for the object, selected from a frozen "
        "precedence list (``boundary`` > ``building`` > ``amenity`` > ...). "
        "``other`` when no recognised key is present."
    ),
    "geometry": (
        "Deterministic GeoJSON ``Polygon`` or ``MultiPolygon`` in WGS84. "
        "Coordinate order is ``[lon, lat]``. Coordinates are rounded to "
        "seven decimal places. Empty geometries are rejected at extraction "
        "time and counted as expected exclusions; they never reach this column."
    ),
    "centroid": (
        "Deterministic GeoJSON ``Point`` in WGS84 (``[lon, lat]``) "
        "representing the polygon's centroid. Coordinates are rounded to "
        "seven decimal places."
    ),
    "centroid_kind": (
        "Identifier of the centroid algorithm used. Currently always "
        '``"lambert_azimuthal_equal_area"`` -- the centroid is computed '
        "in a Lambert azimuthal equal-area projection centred on the "
        "polygon's area-weighted outer-ring barycenter and reprojected "
        "back to WGS84. NOT a geodesic centroid."
    ),
    "lat": (
        "Centroid latitude in WGS84 decimal degrees. Rounded to seven "
        "decimal places. ``NaN`` and infinity are never produced."
    ),
    "lon": (
        "Centroid longitude in WGS84 decimal degrees. Rounded to seven "
        "decimal places. ``NaN`` and infinity are never produced."
    ),
    "bbox": (
        "Deterministic JSON array ``[min_lon, min_lat, max_lon, max_lat]`` "
        "in WGS84 decimal degrees. Coordinates are rounded to seven "
        "decimal places."
    ),
    "area_m2": (
        "Polygon area in square metres computed on the WGS84 ellipsoid "
        "via ``pyproj.Geod``. Outer-ring area minus the absolute area of "
        "every inner ring. Always finite and non-negative."
    ),
    "area_bucket": (
        'Coarse area bucket. One of ``"<10m2"``, ``"10-100m2"``, '
        '``"100m2-1km2"``, ``"1-10km2"``, ``"10-100km2"``, '
        '``"100km2-1000km2"``, ``">=1000km2"``.'
    ),
    "schema_version": (
        "Schema version of the public polygon table. Equal to "
        "``SCHEMA_VERSION`` for every row produced by this pipeline."
    ),
    "website_text": (
        "Full main text extracted from ``website`` with Trafilatura; null unless extraction "
        "succeeds."
    ),
    "website_word_count": (
        "Number of Python Unicode ``\\w+`` sequences in ``website_text``; null without text."
    ),
    "website_text_status": (
        "Website text enrichment status from the documented frozen vocabulary."
    ),
    "contact_website_text": (
        "Full main text extracted independently from ``contact:website`` with Trafilatura; "
        "null unless extraction succeeds."
    ),
    "contact_website_word_count": (
        "Number of Python Unicode ``\\w+`` sequences in ``contact_website_text``; null without text."
    ),
    "contact_website_text_status": (
        "Contact website text enrichment status from the documented frozen vocabulary."
    ),
}


def polygon_column_names(schema: pa.Schema) -> list[str]:
    """Return ``schema``'s column names in declaration order."""
    return [field.name for field in schema]


def column_doc(name: str) -> str:
    """Return the documentation string for column ``name``.

    Raises :class:`KeyError` for unknown columns so the verifier can catch
    typos in callers and schema drift.
    """
    if name not in _COLUMN_DOCS:
        raise KeyError(f"no documentation for column {name!r}")
    return _COLUMN_DOCS[name]


def column_documentation() -> dict[str, str]:
    """Return a copy of the column documentation table for card rendering."""
    return dict(_COLUMN_DOCS)


def _is_non_empty_string(value: object) -> bool:
    """Return whether ``value`` is a non-empty string."""
    return isinstance(value, str) and value != ""


def _require_website_flag(has_website: bool, has_contact_website: bool) -> None:
    """Require at least one website-presence flag."""
    if not (has_website or has_contact_website):
        raise PublicRowInvariantError("row has neither website nor contact:website; rejected")


def _require_any_website_value(website_present: bool, contact_present: bool) -> None:
    """Require at least one non-empty website value."""
    if not (website_present or contact_present):
        raise PublicRowInvariantError(
            "has_* flags claim a website key but the trimmed value is missing"
        )


def _require_website_value(has_website: bool, website_present: bool) -> None:
    """Require a non-empty website when its presence flag is set."""
    if has_website and not website_present:
        raise PublicRowInvariantError("has_website is true but website value is null/empty")


def _require_contact_website_value(has_contact_website: bool, contact_present: bool) -> None:
    """Require a non-empty contact website when its presence flag is set."""
    if has_contact_website and not contact_present:
        raise PublicRowInvariantError(
            "has_contact_website is true but contact_website value is null/empty"
        )


def validate_public_row(row: dict[str, object]) -> None:
    """Validate that ``row`` satisfies the public-shard invariants.

    Raises :class:`PublicRowInvariantError` on any violation.

    Invariants:

    * ``has_any_website`` is ``True``.
    * ``website`` is non-null and non-empty OR ``contact_website`` is
      non-null and non-empty.
    * ``has_website`` matches the presence of a non-empty ``website``.
    * ``has_contact_website`` matches the presence of a non-empty
      ``contact_website``.
    """
    has_ws = bool(row.get("has_website"))
    has_cw = bool(row.get("has_contact_website"))
    _require_website_flag(has_ws, has_cw)
    website_present = _is_non_empty_string(row.get("website"))
    contact_present = _is_non_empty_string(row.get("contact_website"))
    _require_any_website_value(website_present, contact_present)
    _require_website_value(has_ws, website_present)
    _require_contact_website_value(has_cw, contact_present)


__all__ = [
    "POLYGON_PUBLIC_SCHEMA",
    "POLYGON_PUBLIC_SCHEMA_V1_1",
    "POLYGON_PUBLIC_SCHEMA_V1_2",
    "PUBLIC_ROW_INVARIANT_ERROR",
    "SCHEMA_VERSION",
    "PublicRowInvariantError",
    "column_doc",
    "column_documentation",
    "is_supported_public_polygon_schema",
    "polygon_column_names",
    "schema_matches",
    "validate_public_row",
]
