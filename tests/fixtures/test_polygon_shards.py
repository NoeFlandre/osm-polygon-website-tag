from __future__ import annotations

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
from osm_polygon_website_tag.reporting.verification.sentence import verify_sentence_invariants
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


def test_default_v1_5_polygon_row_satisfies_sentence_verifier(tmp_path) -> None:
    row = polygon_row("v1.5")
    path = tmp_path / "polygons" / "source.parquet"
    path.parent.mkdir()
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA_V1_5), path)
    errors: list[str] = []

    verify_sentence_invariants(tmp_path, errors)

    assert errors == []
