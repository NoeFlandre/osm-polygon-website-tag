"""Tests for mixed v1.3/v1.4 public-shard reads."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.storage.duckdb_engine import (
    fresh_connection,
    register_public_parquets,
)


def _row(index: int, *, language: bool) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in POLYGON_PUBLIC_SCHEMA_V1_4:
        if field.name == "polygon_id":
            values[field.name] = f"source:way/{index}"
        elif field.name == "website":
            values[field.name] = "https://example.org"
        elif field.name in {"has_website", "has_any_website"}:
            values[field.name] = True
        elif field.name == "website_text":
            values[field.name] = "text"
        elif field.name == "website_word_count":
            values[field.name] = 1
        elif field.name == "website_text_status":
            values[field.name] = "success"
        elif field.name == "contact_website_text_status":
            values[field.name] = "absent"
        elif field.name == "schema_version":
            values[field.name] = "v1.4" if language else "v1.3"
        elif field.name == "website_language":
            values[field.name] = "eng_Latn" if language else None
        elif field.name == "website_language_probability":
            values[field.name] = 0.9 if language else None
        elif field.name in {
            "contact_website",
            "contact_website_language",
            "contact_website_language_probability",
        }:
            values[field.name] = None
        elif pa.types.is_boolean(field.type):
            values[field.name] = False
        elif pa.types.is_integer(field.type):
            values[field.name] = 0
        elif pa.types.is_floating(field.type):
            values[field.name] = 0.0
        elif pa.types.is_timestamp(field.type):
            values[field.name] = pa.scalar(0, type=field.type).as_py()
        else:
            values[field.name] = ""
    values["has_contact_website"] = False
    return values


def test_register_public_parquets_reads_mixed_current_schemas(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    pq.write_table(
        pa.Table.from_pylist([_row(0, language=False)], schema=POLYGON_PUBLIC_SCHEMA),
        polygons / "a.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([_row(1, language=True)], schema=POLYGON_PUBLIC_SCHEMA_V1_4),
        polygons / "b.parquet",
    )
    connection = fresh_connection(tmp_path)
    try:
        register_public_parquets(connection, polygons)
        rows = connection.execute(
            "SELECT polygon_id, website_language FROM public_polygons ORDER BY polygon_id"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [("source:way/0", None), ("source:way/1", "eng_Latn")]
