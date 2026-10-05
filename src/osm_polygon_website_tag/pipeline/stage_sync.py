"""Shared checks and checkpoint staging for the Grid'5000 stage synchronizers.

The language and sentence stages follow the same lifecycle: stage a source shard
with its checkpoint prefix, run offline, then validate and install the result.
Each stage module keeps only its configuration (label, schema, loader); the
mechanics live here once.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import schema_matches
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint, CheckpointStore
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.runtime.run_state import hash_shard


class _IdentifiedResult(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def model(self) -> ModelIdentity: ...

    @property
    def commit(self) -> str: ...


def copy_checkpoint(
    source: Path,
    target: Path,
    *,
    store: CheckpointStore,
    label: str,
    load: Callable[[], Checkpoint],
) -> None:
    """Validate and copy an existing source-bound checkpoint prefix.

    ``load`` validates the checkpoint against the staged source identity and
    may raise for stage-specific reasons.
    """
    checkpoint_dir = store.directory_for(source)
    if not checkpoint_dir.exists():
        return
    if not checkpoint_dir.is_dir():
        raise ValueError(f"{label} checkpoint is not a directory: {checkpoint_dir}")
    checkpoint = load()
    shutil.copytree(checkpoint.directory, target / checkpoint.directory.name)


def validate_completed_shard(
    path: Path,
    *,
    row_count: int,
    sha256: str,
    schema: pa.Schema,
    label: str,
    name_in_message: bool = False,
) -> None:
    """Validate a completed shard's row count, schema, and digest."""
    suffix = f": {path.name}" if name_in_message else ""
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows != row_count:
        raise ValueError(f"completed {label} shard row count does not match result{suffix}")
    if not schema_matches(parquet.schema_arrow, schema):
        raise ValueError(f"completed {label} shard schema mismatch{suffix}")
    if hash_shard(path) != sha256:
        raise ValueError(f"completed {label} shard hash does not match result{suffix}")


def validate_result_identity(result: _IdentifiedResult, bundle: _IdentifiedResult) -> None:
    """Reject a receipt produced for another run, model, or commit."""
    if result.run_id != bundle.run_id:
        raise ValueError("result run identity does not match bundle")
    if result.model != bundle.model:
        raise ValueError("result model identity does not match bundle")
    if result.commit != bundle.commit:
        raise ValueError("result commit does not match bundle")
