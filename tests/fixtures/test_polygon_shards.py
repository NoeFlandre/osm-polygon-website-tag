from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    POLYGON_PUBLIC_SCHEMA_V1_3,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
)
from osm_polygon_website_tag.reporting.verification.language import verify_language_invariants
from osm_polygon_website_tag.reporting.verification.sentence import verify_sentence_invariants
from osm_polygon_website_tag.reporting.verification.text import verify_text_invariants
from tests.fixtures.polygon_shards import PolygonSchemaVersion, polygon_row


@pytest.mark.parametrize(
    ("version", "schema"),
    [
        ("v1.1", POLYGON_PUBLIC_SCHEMA_V1_1),
        ("v1.2", POLYGON_PUBLIC_SCHEMA_V1_2),
        ("v1.3", POLYGON_PUBLIC_SCHEMA_V1_3),
        ("v1.4", POLYGON_PUBLIC_SCHEMA_V1_4),
        ("v1.5", POLYGON_PUBLIC_SCHEMA_V1_5),
    ],
)
def test_polygon_row_matches_requested_schema(
    version: PolygonSchemaVersion, schema: pa.Schema
) -> None:
    row = polygon_row(version)

    assert set(row) == set(schema.names)
    assert row["schema_version"] == version


def test_polygon_row_applies_known_overrides_and_rejects_unknown_fields() -> None:
    assert polygon_row("v1.4", polygon_id="source:way/42")["polygon_id"] == "source:way/42"
    with pytest.raises(KeyError, match="unknown polygon row fields"):
        polygon_row("v1.4", misspelled_field="value")


@pytest.mark.parametrize(
    ("version", "schema"),
    [("v1.4", POLYGON_PUBLIC_SCHEMA_V1_4), ("v1.5", POLYGON_PUBLIC_SCHEMA_V1_5)],
)
def test_default_language_and_sentence_rows_satisfy_stage_verifiers(
    tmp_path, version: PolygonSchemaVersion, schema: pa.Schema
) -> None:
    row = polygon_row(version)
    path = tmp_path / "polygons" / "source.parquet"
    path.parent.mkdir()
    pq.write_table(pa.Table.from_pylist([row], schema=schema), path)
    errors: list[str] = []

    verify_text_invariants(tmp_path, "complete", errors)
    verify_language_invariants(tmp_path, errors)
    verify_sentence_invariants(tmp_path, errors)

    assert errors == []


@pytest.mark.parametrize(
    ("version", "schema"),
    [("v1.4", POLYGON_PUBLIC_SCHEMA_V1_4), ("v1.5", POLYGON_PUBLIC_SCHEMA_V1_5)],
)
def test_a_missing_contact_website_has_absent_stage_defaults(
    tmp_path: Path, version: PolygonSchemaVersion, schema: pa.Schema
) -> None:
    row = polygon_row(version, contact_website=None)
    path = tmp_path / "polygons" / "source.parquet"
    path.parent.mkdir()
    pq.write_table(pa.Table.from_pylist([row], schema=schema), path)
    errors: list[str] = []

    verify_text_invariants(tmp_path, "complete", errors)
    verify_language_invariants(tmp_path, errors)
    if version == "v1.5":
        verify_sentence_invariants(tmp_path, errors)

    assert errors == []
    assert row["contact_website_text_status"] == "absent"
    assert row["contact_website_language"] is None
    if version == "v1.5":
        assert row["contact_website_sentence_status"] == "absent"
