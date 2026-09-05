"""Checkpoint identity for the language-detection stage."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_SCHEMA_VERSION
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA_V1_4
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint, CheckpointStore
from osm_polygon_website_tag.pipeline.glotlid import ModelIdentity

CHECKPOINT_DIRECTORY_SUFFIX = ".language.parts"


def language_checkpoint_store() -> CheckpointStore:
    """Return the durable checkpoint store bound to the language contract.

    The store is built per call rather than held as a module-level singleton
    because mutmut wraps ``CheckpointStore.__init__``; constructing it during
    module import makes the mutation gate resolve ``<frozen
    importlib._bootstrap>`` as the caller and abort collection.
    """
    return CheckpointStore(
        label="language",
        directory_suffix=CHECKPOINT_DIRECTORY_SUFFIX,
        schema=POLYGON_PUBLIC_SCHEMA_V1_4,
        schema_version=LANGUAGE_SCHEMA_VERSION,
        identity_description="source or model identity",
    )


def load_language_checkpoint(
    shard: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
    model: ModelIdentity,
) -> Checkpoint:
    """Load the durable prefix bound to one shard and the pinned model.

    Detected labels are only reusable while both the source rows and the model
    that produced them are unchanged, so the model identity joins the source
    hash in the stored contract.
    """
    return language_checkpoint_store().load(
        shard,
        source_row_count=source_row_count,
        source_shard_sha256=source_shard_sha256,
        identity={
            "model_repository": model.repository,
            "model_filename": model.filename,
            "model_revision": model.revision,
            "model_sha256": model.sha256,
        },
    )


__all__ = [
    "CHECKPOINT_DIRECTORY_SUFFIX",
    "language_checkpoint_store",
    "load_language_checkpoint",
]
