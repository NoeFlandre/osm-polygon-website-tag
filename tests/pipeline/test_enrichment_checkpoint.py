"""Contract for the enrichment stage's durable checkpoint identity."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pyarrow as pa
import pytest

import osm_polygon_website_tag.pipeline.enrichment_checkpoint as enrichment_checkpoint
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    SCHEMA_VERSION,
)
from osm_polygon_website_tag.pipeline.enrichment_checkpoint import enrichment_checkpoint_store


def _row(index: int) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in POLYGON_PUBLIC_SCHEMA:
        if field.name == "polygon_id":
            values[field.name] = f"source:way/{index}"
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
    values["has_any_website"] = True
    values["has_website"] = True
    values["website"] = "https://example.org"
    values["schema_version"] = "v1.3"
    return values


def test_enrichment_checkpoint_module_exposes_focused_boundary() -> None:
    """Checkpoint identity is isolated from URL-enrichment orchestration."""
    module = importlib.import_module("osm_polygon_website_tag.pipeline.enrichment_checkpoint")

    assert set(module.__all__) == {
        "CHECKPOINT_DIRECTORY_SUFFIX",
        "enrichment_checkpoint_store",
    }


def test_store_defaults_to_the_current_public_polygon_contract() -> None:
    store = enrichment_checkpoint_store()

    assert store.schema.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert store.schema_version == SCHEMA_VERSION


def test_store_binds_to_the_requested_target_contract() -> None:
    store = enrichment_checkpoint_store(POLYGON_PUBLIC_SCHEMA_V1_4, "v1.4")

    assert store.schema.equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
    assert store.schema_version == "v1.4"


def test_checkpoint_parts_live_beside_the_shard_they_enrich(tmp_path: Path) -> None:
    shard = tmp_path / "polygons" / "region.parquet"

    directory = enrichment_checkpoint_store().directory_for(shard)

    assert directory == tmp_path / "polygons" / ".region.parquet.enriching.parts"
    assert enrichment_checkpoint.CHECKPOINT_DIRECTORY_SUFFIX == ".enriching.parts"


def test_load_binds_the_prefix_to_the_source_shard(tmp_path: Path) -> None:
    shard = tmp_path / "nested" / "region.parquet"
    store = enrichment_checkpoint_store(POLYGON_PUBLIC_SCHEMA, "v9")

    loaded = store.load(shard, source_row_count=7, source_shard_sha256="b" * 64)

    assert json.loads((loaded.directory / "checkpoint.json").read_text()) == {
        "checkpoint_version": 1,
        "schema_version": "v9",
        "source_row_count": 7,
        "source_shard_sha256": "b" * 64,
    }
    with pytest.raises(
        ValueError,
        match=re.escape("enrichment checkpoint does not match source shard: region.parquet"),
    ):
        store.load(shard, source_row_count=8, source_shard_sha256="b" * 64)


def test_stage_errors_name_the_enrichment_stage(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    store = enrichment_checkpoint_store()
    loaded = store.load(shard, source_row_count=1, source_shard_sha256="a" * 64)
    store.write_part(loaded.directory, 0, [_row(1)], batch_rows=1)

    with pytest.raises(
        ValueError,
        match=re.escape("enrichment checkpoint part already exists: part-00000000.parquet"),
    ):
        store.write_part(loaded.directory, 0, [_row(2)], batch_rows=1)
    with pytest.raises(ValueError, match="enrichment row count changed while assembling"):
        store.assemble(
            store.parts(loaded.directory),
            tmp_path / "staged.parquet",
            batch_rows=1,
            row_count=2,
        )
