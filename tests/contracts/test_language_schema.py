"""Tests for the optional v1.4 language fields."""

from __future__ import annotations

import pyarrow as pa

from osm_polygon_website_tag.contracts.language_schema import (
    LANGUAGE_COLUMN_NAMES,
    LANGUAGE_FIELDS,
    LANGUAGE_SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    is_current_public_polygon_schema,
    is_supported_public_polygon_schema,
)


def test_language_contract_is_nullable_and_ordered() -> None:
    assert LANGUAGE_SCHEMA_VERSION == "v1.4"
    assert LANGUAGE_COLUMN_NAMES == (
        "website_language",
        "website_language_probability",
        "contact_website_language",
        "contact_website_language_probability",
    )
    assert [field.name for field in LANGUAGE_FIELDS] == list(LANGUAGE_COLUMN_NAMES)
    assert all(field.nullable for field in LANGUAGE_FIELDS)
    assert str(LANGUAGE_FIELDS[0].type) == "string"
    assert str(LANGUAGE_FIELDS[1].type) == "double"


def test_v1_4_extends_v1_3_and_default_schema_stays_v1_3() -> None:
    assert isinstance(POLYGON_PUBLIC_SCHEMA, pa.Schema)
    assert POLYGON_PUBLIC_SCHEMA_V1_4.names[: len(POLYGON_PUBLIC_SCHEMA.names)] == list(
        POLYGON_PUBLIC_SCHEMA.names
    )
    assert POLYGON_PUBLIC_SCHEMA.names[-1] == "contact_website_text_status"
    assert POLYGON_PUBLIC_SCHEMA_V1_4.names[-4:] == list(LANGUAGE_COLUMN_NAMES)
    assert is_current_public_polygon_schema(POLYGON_PUBLIC_SCHEMA)
    assert is_current_public_polygon_schema(POLYGON_PUBLIC_SCHEMA_V1_4)
    assert is_supported_public_polygon_schema(POLYGON_PUBLIC_SCHEMA_V1_4)
