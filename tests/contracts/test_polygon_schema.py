"""Tests for the public polygon schema."""

from __future__ import annotations

import pyarrow as pa
import pytest

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    SCHEMA_VERSION,
    PublicRowInvariantError,
    column_doc,
    column_documentation,
    is_supported_public_polygon_schema,
    polygon_column_names,
    schema_matches,
    validate_public_row,
)


def test_schema_version_is_v1_3() -> None:
    assert SCHEMA_VERSION == "v1.3"


def test_polygon_public_schema_is_arrow_schema() -> None:
    assert isinstance(POLYGON_PUBLIC_SCHEMA, pa.Schema)


@pytest.mark.parametrize(
    "schema",
    [POLYGON_PUBLIC_SCHEMA_V1_1, POLYGON_PUBLIC_SCHEMA_V1_2, POLYGON_PUBLIC_SCHEMA],
)
def test_supported_public_polygon_schema_accepts_known_versions(schema: pa.Schema) -> None:
    assert is_supported_public_polygon_schema(schema)


def test_supported_public_polygon_schema_rejects_unknown_columns() -> None:
    unsupported = POLYGON_PUBLIC_SCHEMA.append(pa.field("unexpected", pa.string()))
    assert not is_supported_public_polygon_schema(unsupported)


def test_schema_matches_requires_exact_metadata() -> None:
    with_metadata = POLYGON_PUBLIC_SCHEMA.with_metadata({b"fixture": b"metadata"})
    assert not schema_matches(with_metadata, POLYGON_PUBLIC_SCHEMA)
    assert schema_matches(POLYGON_PUBLIC_SCHEMA, POLYGON_PUBLIC_SCHEMA)


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
    assert {
        "preferred_website",
        "preferred_website_source",
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "area_km2",
    }.isdisjoint(names)


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


def test_polygon_public_schema_website_flags_not_nullable() -> None:
    by_name = {f.name: f for f in POLYGON_PUBLIC_SCHEMA}
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


def test_column_doc_returns_string_per_column() -> None:
    for col in polygon_column_names(POLYGON_PUBLIC_SCHEMA):
        doc = column_doc(col)
        assert isinstance(doc, str)
        assert len(doc) > 0


def test_column_doc_unknown_raises() -> None:
    with pytest.raises(KeyError) as exc_info:
        column_doc("not_a_real_column")
    assert exc_info.value.args == ("no documentation for column 'not_a_real_column'",)


def test_column_documentation_returns_an_independent_copy() -> None:
    docs = column_documentation()
    docs.pop("geometry")

    assert "geometry" in column_documentation()


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
    validate_public_row(row)


def test_validate_public_row_accepts_both_website_keys() -> None:
    row = _valid_row()
    row["contact_website"] = "https://contact.example"
    row["has_contact_website"] = True
    row["contact_website_class"] = "absolute_url"
    row["contact_website_hostname"] = "contact.example"
    validate_public_row(row)


def test_validate_public_row_rejects_neither_website_key() -> None:
    row = _valid_row()
    row["website"] = None
    row["contact_website"] = None
    row["has_website"] = False
    row["has_contact_website"] = False
    row["has_any_website"] = False
    with pytest.raises(PublicRowInvariantError) as exc_info:
        validate_public_row(row)
    assert str(exc_info.value) == "row has neither website nor contact:website; rejected"


def test_validate_public_row_rejects_has_flag_mismatch() -> None:
    row = _valid_row()
    row["has_website"] = False  # but website is present
    with pytest.raises(PublicRowInvariantError):
        validate_public_row(row)


def test_validate_public_row_rejects_present_flag_with_no_value() -> None:
    row = _valid_row()
    row["website"] = None
    row["contact_website"] = None
    row["has_website"] = True
    row["has_contact_website"] = False
    with pytest.raises(PublicRowInvariantError) as exc_info:
        validate_public_row(row)
    assert str(exc_info.value) == "has_* flags claim a website key but the trimmed value is missing"


def test_validate_public_row_rejects_website_flag_without_website_value() -> None:
    row = _valid_row()
    row["website"] = ""
    row["contact_website"] = "https://contact.example"
    row["has_website"] = True
    row["has_contact_website"] = True
    with pytest.raises(PublicRowInvariantError) as exc_info:
        validate_public_row(row)
    assert str(exc_info.value) == "has_website is true but website value is null/empty"


def test_validate_public_row_rejects_contact_flag_without_contact_value() -> None:
    row = _valid_row()
    row["contact_website"] = ""
    row["has_contact_website"] = True
    with pytest.raises(PublicRowInvariantError) as exc_info:
        validate_public_row(row)
    assert (
        str(exc_info.value) == "has_contact_website is true but contact_website value is null/empty"
    )
