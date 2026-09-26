"""Contract for the language stage's durable checkpoint identity."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest
from tests.fixtures.polygon_shards import polygon_row

import osm_polygon_website_tag.pipeline.language_detection_checkpoint as language_checkpoint
from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_SCHEMA_VERSION
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA_V1_4
from osm_polygon_website_tag.pipeline.glotlid import ModelIdentity
from osm_polygon_website_tag.pipeline.language_detection_checkpoint import (
    language_checkpoint_store,
    load_language_checkpoint,
)


def _model(sha256: str = "a" * 64) -> ModelIdentity:
    return ModelIdentity("cis-lmu/glotlid", "model_v3.bin", "85cd671", sha256)


def _row(index: int) -> dict[str, object]:
    return polygon_row(
        "v1.4",
        polygon_id=f"source:way/{index}",
        website_text=f"text {index}",
        website_word_count=2,
        website_text_status="success",
        website_language=None,
        website_language_probability=None,
    )


def test_language_checkpoint_module_exposes_focused_boundary() -> None:
    """Checkpoint identity is isolated from detection orchestration."""
    module = importlib.import_module(
        "osm_polygon_website_tag.pipeline.language_detection_checkpoint"
    )

    assert set(module.__all__) == {
        "CHECKPOINT_DIRECTORY_SUFFIX",
        "language_checkpoint_store",
        "load_language_checkpoint",
    }


def test_store_is_bound_to_the_v1_4_language_contract() -> None:
    assert language_checkpoint_store().schema.equals(
        POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True
    )
    assert language_checkpoint_store().schema_version == LANGUAGE_SCHEMA_VERSION


def test_checkpoint_parts_live_beside_the_shard_they_label(tmp_path: Path) -> None:
    shard = tmp_path / "polygons" / "region.parquet"

    directory = language_checkpoint_store().directory_for(shard)

    assert directory == tmp_path / "polygons" / ".region.parquet.language.parts"
    assert language_checkpoint.CHECKPOINT_DIRECTORY_SUFFIX == ".language.parts"


def test_load_binds_the_prefix_to_both_source_and_model(tmp_path: Path) -> None:
    shard = tmp_path / "nested" / "region.parquet"

    loaded = load_language_checkpoint(
        shard,
        source_row_count=4,
        source_shard_sha256="b" * 64,
        model=_model(),
    )

    assert json.loads((loaded.directory / "checkpoint.json").read_text()) == {
        "checkpoint_version": 1,
        "schema_version": "v1.4",
        "source_row_count": 4,
        "source_shard_sha256": "b" * 64,
        "model_repository": "cis-lmu/glotlid",
        "model_filename": "model_v3.bin",
        "model_revision": "85cd671",
        "model_sha256": "a" * 64,
    }
    assert loaded.completed_rows == 0


def test_load_rejects_source_or_model_drift(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    load_language_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=_model(),
    )
    drift = re.escape("language checkpoint does not match source or model identity: region.parquet")

    with pytest.raises(ValueError, match=drift):
        load_language_checkpoint(
            shard,
            source_row_count=1,
            source_shard_sha256="c" * 64,
            model=_model(),
        )
    with pytest.raises(ValueError, match=drift):
        load_language_checkpoint(
            shard,
            source_row_count=1,
            source_shard_sha256="b" * 64,
            model=_model("d" * 64),
        )
    with pytest.raises(ValueError, match=drift):
        load_language_checkpoint(
            shard,
            source_row_count=1,
            source_shard_sha256="b" * 64,
            model=ModelIdentity("other/repo", "other.bin", "0000000", "a" * 64),
        )


def test_load_reports_a_durable_prefix_written_for_the_same_model(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    opened = load_language_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=_model(),
    )
    language_checkpoint_store().write_part(opened.directory, 0, [_row(0)], batch_rows=1)

    loaded = load_language_checkpoint(
        shard,
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=_model(),
    )

    assert loaded.completed_rows == 1


def test_stage_errors_name_the_language_stage(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    language_checkpoint_store().write_part(directory, 0, [_row(0)], batch_rows=1)

    with pytest.raises(
        ValueError,
        match=re.escape("language checkpoint part already exists: part-00000000.parquet"),
    ):
        language_checkpoint_store().write_part(directory, 0, [_row(1)], batch_rows=1)
    with pytest.raises(ValueError, match="language row count changed while assembling"):
        language_checkpoint_store().assemble(
            language_checkpoint_store().parts(directory),
            tmp_path / "staged.parquet",
            batch_rows=1,
            row_count=2,
        )
