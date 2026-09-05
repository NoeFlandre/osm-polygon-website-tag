"""Checkpoint identity for the polygon-enrichment stage."""

from __future__ import annotations

import pyarrow as pa

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    SCHEMA_VERSION,
)
from osm_polygon_website_tag.pipeline.checkpoint_storage import CheckpointStore

CHECKPOINT_DIRECTORY_SUFFIX = ".enriching.parts"


def enrichment_checkpoint_store(
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
    schema_version: str = SCHEMA_VERSION,
) -> CheckpointStore:
    """Return the durable checkpoint store bound to one enrichment contract.

    Enrichment migrates a shard to whichever public schema its source supports,
    so the store is built per invocation from that resolved target contract.
    """
    return CheckpointStore(
        label="enrichment",
        directory_suffix=CHECKPOINT_DIRECTORY_SUFFIX,
        schema=schema,
        schema_version=schema_version,
        identity_description="source shard",
    )


__all__ = ["CHECKPOINT_DIRECTORY_SUFFIX", "enrichment_checkpoint_store"]
