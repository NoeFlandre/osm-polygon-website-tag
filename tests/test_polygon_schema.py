"""Tests for the public polygon schema."""

from __future__ import annotations

import pyarrow as pa
import pytest

from osm_polygon_website_tag.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    SCHEMA_VERSION,
    PublicRowInvariantError,
    column_doc,
    polygon_column_names,
    validate_public_row,
)


def test_schema_version_is_v1_2() -> None:
    assert SCHEMA_VERSION == "v1.2"


def test_polygon_public_schema_is_arrow_schema() -> None:
    assert isinstance(POLYGON_PUBLIC_SCHEMA, pa.Schema)


def test_polygon_public_schema_required_columns() -> None:
    required = [
        "polygon_id",
        "region",
        "source_pbf",
        "osm_type",
        "osm_id",
        "osm_version",
        "osm_timestamp",
        "website",
        "contact_website",
        "has_website",
        "has_contact_website",
        "has_any_website",
        "website_class",
        "contact_website_class",
        "website_hostname",
        "contact_website_hostname",
        "preferred_website",
        "preferred_website_source",
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "name",
        "tags",
        "tag_keys",
        "tag_count",
        "osm_primary_tag",
        "geometry",
        "centroid",
        "centroid_kind",
        "lat",
        "lon",
        "bbox",
        "area_m2",
        "area_km2",
        "area_bucket",
        "schema_version",
        "website_text",
        "website_word_count",
        "website_text_status",
        "contact_website_text",
        "contact_website_word_count",
        "contact_website_text_status",
    ]
    names = polygon_column_names(POLYGON_PUBLIC_SCHEMA)
    for col in required:
        assert col in names, f"missing column {col} in {names}"


def test_polygon_public_schema_extracted_at_replaced_by_schema_version() -> None:
    """v1.1 has no per-row extracted_at; the schema version is on every row."""
    names = polygon_column_names(POLYGON_PUBLIC_SCHEMA)
    assert "extracted_at" not in names
    assert "extraction_version" not in names
    assert "schema_version" in names


def test_polygon_public_schema_nullable_columns() -> None:
    """Nullability is part of the contract."""
    by_name = {f.name: f for f in POLYGON_PUBLIC_SCHEMA}
    nullable = {
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "name",
        "website",
        "contact_website",
        "website_class",
        "contact_website_class",
        "website_hostname",
        "contact_website_hostname",
    }
    for col in nullable:
        assert by_name[col].nullable is True, f"{col} must be nullable"


def test_polygon_public_schema_preferred_website_not_nullable() -> None:
    """preferred_website and its source are always present in public rows."""
    by_name = {f.name: f for f in POLYGON_PUBLIC_SCHEMA}
    assert by_name["preferred_website"].nullable is False
    assert by_name["preferred_website_source"].nullable is False
    assert by_name["has_any_website"].nullable is False
    assert by_name["has_website"].nullable is False
    assert by_name["has_contact_website"].nullable is False


def test_polygon_public_schema_geometry_type() -> None:
    by_name = {f.name: f for f in POLYGON_PUBLIC_SCHEMA}
    geom_type = by_name["geometry"].type
    assert pa.types.is_string(geom_type) or pa.types.is_large_string(geom_type)


def test_polygon_public_schema_geometry_must_be_polygon_or_multipolygon() -> None:
    doc = column_doc("geometry")
    assert "Polygon" in doc
    assert "MultiPolygon" in doc


def test_polygon_public_schema_centroid_kind_documents_algorithm() -> None:
    doc = column_doc("centroid_kind")
    assert "Lambert" in doc or "lambert" in doc
    assert "equal-area" in doc.lower() or "equal_area" in doc.lower()


def test_polygon_public_schema_dtypes() -> None:
    by_name = {f.name: f for f in POLYGON_PUBLIC_SCHEMA}
    assert pa.types.is_string(by_name["polygon_id"].type)
    assert pa.types.is_boolean(by_name["has_website"].type)
    assert pa.types.is_boolean(by_name["has_contact_website"].type)
    assert pa.types.is_boolean(by_name["has_any_website"].type)
    assert pa.types.is_int64(by_name["osm_id"].type)
    assert pa.types.is_int32(by_name["osm_version"].type)
    assert pa.types.is_timestamp(by_name["osm_timestamp"].type)
    assert pa.types.is_int32(by_name["tag_count"].type)
    assert pa.types.is_float64(by_name["lat"].type)
    assert pa.types.is_float64(by_name["lon"].type)
    assert pa.types.is_float64(by_name["area_m2"].type)
    assert pa.types.is_float64(by_name["area_km2"].type)


def test_column_doc_returns_string_per_column() -> None:
    for col in polygon_column_names(POLYGON_PUBLIC_SCHEMA):
        doc = column_doc(col)
        assert isinstance(doc, str)
        assert len(doc) > 0


def test_column_doc_unknown_raises() -> None:
    with pytest.raises(KeyError):
        column_doc("not_a_real_column")


def _valid_row() -> dict[str, object]:
    return {
        "polygon_id": "monaco-latest:way/100",
        "region": "monaco",
        "source_pbf": "monaco-latest.osm.pbf",
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py(),
        "name": None,
        "website": "https://example.com",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.com",
        "contact_website_hostname": None,
        "preferred_website": "https://example.com",
        "preferred_website_source": "website",
        "wikidata": None,
        "wikidata_qid": None,
        "wikidata_class": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": "building",
        "geometry": '{"type":"Polygon","coordinates":[]}',
        "centroid": '{"type":"Point","coordinates":[0,0]}',
        "centroid_kind": "lambert_azimuthal_equal_area",
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0.0,0.0,0.0,0.0]",
        "area_m2": 0.0,
        "area_km2": 0.0,
        "area_bucket": "<10m2",
        "schema_version": SCHEMA_VERSION,
    }


def test_validate_public_row_accepts_valid_row() -> None:
    validate_public_row(_valid_row())


def test_validate_public_row_accepts_contact_website_only() -> None:
    row = _valid_row()
    row["website"] = None
    row["contact_website"] = "https://contact.example"
    row["has_website"] = False
    row["has_contact_website"] = True
    row["website_class"] = None
    row["contact_website_class"] = "absolute_url"
    row["website_hostname"] = None
    row["contact_website_hostname"] = "contact.example"
    row["preferred_website"] = "https://contact.example"
    row["preferred_website_source"] = "contact:website"
    validate_public_row(row)


def test_validate_public_row_accepts_both_website_keys_prefers_website() -> None:
    row = _valid_row()
    row["contact_website"] = "https://contact.example"
    row["has_contact_website"] = True
    row["contact_website_class"] = "absolute_url"
    row["contact_website_hostname"] = "contact.example"
    # preferred remains website when both present
    validate_public_row(row)


def test_validate_public_row_rejects_neither_website_key() -> None:
    row = _valid_row()
    row["website"] = None
    row["contact_website"] = None
    row["has_website"] = False
    row["has_contact_website"] = False
    row["has_any_website"] = False
    row["preferred_website"] = ""
    row["preferred_website_source"] = "website"
    with pytest.raises(PublicRowInvariantError):
        validate_public_row(row)


def test_validate_public_row_rejects_mismatched_preferred_source() -> None:
    row = _valid_row()
    row["preferred_website_source"] = "contact:website"
    row["preferred_website"] = "https://contact.example"
    # website is the actual chosen key here; mismatch rejected.
    with pytest.raises(PublicRowInvariantError):
        validate_public_row(row)


def test_validate_public_row_rejects_has_flag_mismatch() -> None:
    row = _valid_row()
    row["has_website"] = False  # but website is present
    with pytest.raises(PublicRowInvariantError):
        validate_public_row(row)


def test_validate_public_row_rejects_empty_preferred() -> None:
    row = _valid_row()
    row["preferred_website"] = ""
    with pytest.raises(PublicRowInvariantError):
        validate_public_row(row)


def test_validate_public_row_rejects_unknown_source() -> None:
    row = _valid_row()
    row["preferred_website_source"] = "url"
    with pytest.raises(PublicRowInvariantError):
        validate_public_row(row)
