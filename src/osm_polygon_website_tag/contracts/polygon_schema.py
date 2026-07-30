"""Public polygon schema and column documentation.

Schema version: ``v1.2``.

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

Because every public row carries at least one website key, the
derived convenience fields are non-null:

* ``preferred_website`` -- non-null.
* ``preferred_website_source`` -- non-null; exactly ``"website"`` or
  ``"contact:website"``.

The individual website values, classes, hostnames, Wikidata value,
and name remain nullable.

Schema versioning
-----------------

The schema version is part of the dataset contract. A new schema MUST be
introduced with a new ``SCHEMA_VERSION`` (and a new column order) rather
than a silent in-place change. The verifier rejects schema drift.
"""

from __future__ import annotations

import pyarrow as pa

from osm_polygon_website_tag.contracts.text_schema import TEXT_FIELDS

SCHEMA_VERSION = "v1.2"


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

POLYGON_PUBLIC_SCHEMA: pa.Schema = pa.schema([*POLYGON_PUBLIC_SCHEMA_V1_1, *TEXT_FIELDS])


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
    "preferred_website": (
        "Convenience field derived from the two website keys. Equals "
        "the trimmed ``website`` value when present, else the trimmed "
        "``contact:website`` value. Always non-null in public rows."
    ),
    "preferred_website_source": (
        "Tag key chosen by the ``preferred_website`` rule. Always "
        'exactly ``"website"`` or ``"contact:website"`` in public rows.'
    ),
    "wikidata": (
        "Trimmed original ``wikidata`` tag value. ``None`` when the tag "
        "is absent. Malformed-but-non-empty values are retained verbatim."
    ),
    "wikidata_qid": (
        'Canonical single QID (e.g. ``"Q42"``) when the trimmed '
        "``wikidata`` value parses to exactly one canonical QID; "
        "``None`` otherwise (multiple, malformed, or absent)."
    ),
    "wikidata_class": (
        "Discrete classification of the wikidata value: "
        "``canonical_qid``, ``multiple``, ``malformed``. ``None`` when the "
        "tag is absent."
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
    "area_km2": ("Polygon area in square kilometres. Always finite and non-negative."),
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


_PREFERRED_SOURCES = ("website", "contact:website")


def validate_public_row(row: dict[str, object]) -> None:
    """Validate that ``row`` satisfies the public-shard invariants.

    Raises :class:`PublicRowInvariantError` on any violation.

    Invariants:

    * ``has_any_website`` is ``True``.
    * ``website`` is non-null and non-empty OR ``contact_website`` is
      non-null and non-empty.
    * ``preferred_website`` is non-null and non-empty.
    * ``preferred_website_source`` is exactly ``"website"`` or
      ``"contact:website"``.
    * ``preferred_website`` equals the chosen original tag value.
    * ``has_website`` matches the presence of a non-empty ``website``.
    * ``has_contact_website`` matches the presence of a non-empty
      ``contact_website``.
    """
    has_ws = bool(row.get("has_website"))
    has_cw = bool(row.get("has_contact_website"))
    if not (has_ws or has_cw):
        raise PublicRowInvariantError("row has neither website nor contact:website; rejected")

    website = row.get("website")
    contact = row.get("contact_website")
    website_present = isinstance(website, str) and website != ""
    contact_present = isinstance(contact, str) and contact != ""
    if not (website_present or contact_present):
        raise PublicRowInvariantError(
            "has_* flags claim a website key but the trimmed value is missing"
        )
    if has_ws and not website_present:
        raise PublicRowInvariantError("has_website is true but website value is null/empty")
    if has_cw and not contact_present:
        raise PublicRowInvariantError(
            "has_contact_website is true but contact_website value is null/empty"
        )

    preferred = row.get("preferred_website")
    if not (isinstance(preferred, str) and preferred):
        raise PublicRowInvariantError("preferred_website must be non-empty")
    source = row.get("preferred_website_source")
    if source not in _PREFERRED_SOURCES:
        raise PublicRowInvariantError(
            f"preferred_website_source must be one of {_PREFERRED_SOURCES}; got {source!r}"
        )
    if source == "website":
        if preferred != website:
            raise PublicRowInvariantError(
                "preferred_website must equal website when source is 'website'"
            )
    else:
        if preferred != contact:
            raise PublicRowInvariantError(
                "preferred_website must equal contact_website when source is 'contact:website'"
            )


__all__ = [
    "POLYGON_PUBLIC_SCHEMA",
    "POLYGON_PUBLIC_SCHEMA_V1_1",
    "PUBLIC_ROW_INVARIANT_ERROR",
    "SCHEMA_VERSION",
    "PublicRowInvariantError",
    "column_doc",
    "column_documentation",
    "polygon_column_names",
    "validate_public_row",
]
