"""Contract for the sentence stage's durable checkpoint identity."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import osm_polygon_website_tag.pipeline.sentence_checkpoint as sentence_checkpoint
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA_V1_5
from osm_polygon_website_tag.contracts.sentence_schema import SENTENCE_SCHEMA_VERSION
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.pipeline.sentence_checkpoint import (
    load_sentence_checkpoint,
    sentence_checkpoint_store,
)


def _model(sha256: str = "a" * 64) -> ModelIdentity:
    return ModelIdentity("segment-any-text/sat-3l-sm", "sat-3l-sm", "abc1234", sha256)


def test_module_exposes_a_focused_boundary() -> None:
    assert set(sentence_checkpoint.__all__) == {
        "CHECKPOINT_DIRECTORY_SUFFIX",
        "load_sentence_checkpoint",
        "sentence_checkpoint_store",
    }


def test_store_is_bound_to_the_v1_5_sentence_contract() -> None:
    store = sentence_checkpoint_store()

    assert store.schema.equals(POLYGON_PUBLIC_SCHEMA_V1_5, check_metadata=True)
    assert store.schema_version == SENTENCE_SCHEMA_VERSION


def test_checkpoint_parts_live_beside_the_shard_they_segment(tmp_path: Path) -> None:
    shard = tmp_path / "polygons" / "region.parquet"

    directory = sentence_checkpoint_store().directory_for(shard)

    assert directory == tmp_path / "polygons" / ".region.parquet.sentences.parts"
    assert sentence_checkpoint.CHECKPOINT_DIRECTORY_SUFFIX == ".sentences.parts"


def test_load_binds_the_prefix_to_both_source_and_model(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"

    loaded = load_sentence_checkpoint(
        shard, source_row_count=4, source_shard_sha256="b" * 64, model=_model()
    )

    assert json.loads((loaded.directory / "checkpoint.json").read_text()) == {
        "checkpoint_version": 1,
        "schema_version": "v1.5",
        "source_row_count": 4,
        "source_shard_sha256": "b" * 64,
        "model_repository": "segment-any-text/sat-3l-sm",
        "model_filename": "sat-3l-sm",
        "model_revision": "abc1234",
        "model_sha256": "a" * 64,
    }
    assert loaded.completed_rows == 0


def test_load_rejects_a_different_segmentation_model(tmp_path: Path) -> None:
    """Sentences are only reusable while the model that produced them is unchanged."""
    shard = tmp_path / "region.parquet"
    load_sentence_checkpoint(
        shard, source_row_count=1, source_shard_sha256="b" * 64, model=_model()
    )
    drift = re.escape("sentence checkpoint does not match source or model identity: region.parquet")

    with pytest.raises(ValueError, match=drift):
        load_sentence_checkpoint(
            shard, source_row_count=1, source_shard_sha256="b" * 64, model=_model("d" * 64)
        )
    with pytest.raises(ValueError, match=drift):
        load_sentence_checkpoint(
            shard, source_row_count=1, source_shard_sha256="c" * 64, model=_model()
        )


def test_stage_errors_name_the_sentence_stage(tmp_path: Path) -> None:
    shard = tmp_path / "region.parquet"
    loaded = load_sentence_checkpoint(
        shard, source_row_count=1, source_shard_sha256="b" * 64, model=_model()
    )
    (loaded.directory / "unexpected").write_text("x")

    with pytest.raises(
        ValueError,
        match=rf"^{re.escape("unrecognized sentence checkpoint contents: ['unexpected']")}$",
    ):
        load_sentence_checkpoint(
            shard, source_row_count=1, source_shard_sha256="b" * 64, model=_model()
        )
