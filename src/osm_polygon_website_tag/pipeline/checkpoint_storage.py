"""Durable, source-bound Parquet checkpoint storage for resumable stages.

A :class:`CheckpointStore` owns everything a resumable stage needs to survive
an interruption: where its durable parts live, how they are named and ordered,
which source identity they are bound to, and how a validated prefix is
assembled back into one shard.  A stage declares its contract once and then
works in whole checkpoints, never in part files, temporary names, metadata
keys, or per-call schema and label arguments.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.runtime.run_state import atomic_write_json
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink

CHECKPOINT_METADATA_NAME = "checkpoint.json"
CHECKPOINT_VERSION = 1

_NO_STAGE_IDENTITY: Mapping[str, object] = MappingProxyType({})


@dataclass(frozen=True)
class Checkpoint:
    """Durable prefix of one checkpointed shard."""

    directory: Path
    parts: tuple[Path, ...]
    completed_rows: int


# Deliberately a plain class rather than a dataclass: mutmut skips decorated
# class definitions outright, so decorating this one would silently drop every
# method below out of the mutation gate.
class CheckpointStore:
    """Durable checkpoint storage bound to one stage's output contract."""

    def __init__(
        self,
        *,
        label: str,
        directory_suffix: str,
        schema: pa.Schema,
        schema_version: str,
        identity_description: str,
    ) -> None:
        self.label = label
        self.directory_suffix = directory_suffix
        self.schema = schema
        self.schema_version = schema_version
        self.identity_description = identity_description

    def directory_for(self, shard: Path) -> Path:
        """Return the source-scoped directory holding this stage's parts."""
        return shard.with_name(f".{shard.name}{self.directory_suffix}")

    def load(
        self,
        shard: Path,
        *,
        source_row_count: int,
        source_shard_sha256: str,
        identity: Mapping[str, object] = _NO_STAGE_IDENTITY,
    ) -> Checkpoint:
        """Open the identity-bound durable prefix already written for a shard.

        The directory is created when absent, so an interrupted run and a first
        run reach the same state.  Anything that is not a recognized part or the
        matching identity contract is rejected rather than silently reused.
        """
        directory = self.directory_for(shard)
        directory.mkdir(parents=True, exist_ok=True)
        _clear_temporaries(directory)
        self._bind_identity(
            directory,
            shard=shard,
            source_row_count=source_row_count,
            source_shard_sha256=source_shard_sha256,
            identity=identity,
        )
        parts = self.parts(directory)
        self._reject_unknown_files(directory, parts)
        completed_rows = sum(pq.ParquetFile(part).metadata.num_rows for part in parts)
        if completed_rows > source_row_count:
            raise ValueError(f"{self.label} checkpoint exceeds source row count: {shard.name}")
        return Checkpoint(directory, parts, completed_rows)

    def parts(self, directory: Path) -> tuple[Path, ...]:
        """Return the sequential, non-empty, schema-bound parts written so far."""
        found = sorted(directory.glob("part-*.parquet"))
        for index, part in enumerate(found):
            if part.name != _part_path(directory, index).name:
                raise ValueError(f"non-sequential {self.label} checkpoint part: {part.name}")
            parquet = pq.ParquetFile(part)
            if not parquet.schema_arrow.equals(self.schema, check_metadata=True):
                raise ValueError(f"invalid {self.label} checkpoint schema: {part.name}")
            if parquet.metadata.num_rows < 1:
                raise ValueError(f"empty {self.label} checkpoint part: {part.name}")
        return tuple(found)

    def write_part(
        self,
        directory: Path,
        index: int,
        rows: list[dict[str, object]],
        *,
        batch_rows: int,
    ) -> None:
        """Publish one completed batch as the next durable part, atomically."""
        if not rows:
            return
        target = _part_path(directory, index)
        if target.exists():
            raise ValueError(f"{self.label} checkpoint part already exists: {target.name}")
        temporary = directory / f".{target.name}.writing"
        sink = BatchParquetSink(temporary, self.schema, batch_rows=batch_rows)
        try:
            for row in rows:
                sink.add(row)
            sink.close()
            self._validate_part(temporary, sink.row_count, len(rows))
            atomic_promote_bundle([(temporary, target)])
        finally:
            sink.close()
            temporary.unlink(missing_ok=True)

    def assemble(
        self,
        parts: tuple[Path, ...],
        staged: Path,
        *,
        batch_rows: int,
        row_count: int,
    ) -> int:
        """Stream durable parts into one validated staged shard.

        Parts already hold Arrow record batches in the target schema, so they
        are copied batch by batch instead of round-tripping through Python
        dictionaries.  The widest batch written is returned so callers can keep
        reporting the row-group bound they observed.
        """
        staged.unlink(missing_ok=True)
        try:
            with pq.ParquetWriter(staged, self.schema, compression="snappy") as writer:
                assembled_rows, max_batch_rows = _stream_parts(writer, parts, batch_rows=batch_rows)
            self._validate_assembled(staged, assembled_rows, row_count)
            return max_batch_rows
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _bind_identity(
        self,
        directory: Path,
        *,
        shard: Path,
        source_row_count: int,
        source_shard_sha256: str,
        identity: Mapping[str, object],
    ) -> None:
        """Validate the stored identity contract or write it for a fresh prefix."""
        expected: dict[str, object] = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "schema_version": self.schema_version,
            "source_row_count": source_row_count,
            "source_shard_sha256": source_shard_sha256,
            **identity,
        }
        metadata_path = directory / CHECKPOINT_METADATA_NAME
        if metadata_path.exists():
            if json.loads(metadata_path.read_bytes()) != expected:
                raise ValueError(
                    f"{self.label} checkpoint does not match "
                    f"{self.identity_description}: {shard.name}"
                )
            return
        if any(directory.iterdir()):
            raise ValueError(f"unrecognized {self.label} checkpoint contents: {directory}")
        atomic_write_json(metadata_path, expected)

    def _reject_unknown_files(self, directory: Path, parts: tuple[Path, ...]) -> None:
        """Refuse to resume from a directory holding files this store never wrote."""
        allowed = {CHECKPOINT_METADATA_NAME, *(part.name for part in parts)}
        unknown = sorted(child.name for child in directory.iterdir() if child.name not in allowed)
        if unknown:
            raise ValueError(f"unrecognized {self.label} checkpoint contents: {unknown}")

    def _validate_part(self, path: Path, actual_rows: int, expected_rows: int) -> None:
        """Validate one durable part before it is promoted into the prefix."""
        if actual_rows != expected_rows:
            raise ValueError(f"{self.label} checkpoint row count changed")
        if not pq.read_schema(path).equals(self.schema, check_metadata=True):
            raise ValueError(f"{self.label} checkpoint schema mismatch")

    def _validate_assembled(self, staged: Path, actual_rows: int, expected_rows: int) -> None:
        """Validate the assembled shard before it can be promoted."""
        if actual_rows != expected_rows:
            raise ValueError(f"{self.label} row count changed while assembling checkpoint")
        if not pq.read_schema(staged).equals(self.schema, check_metadata=True):
            raise ValueError(f"assembled {self.label} schema mismatch")


def _clear_temporaries(directory: Path) -> None:
    """Remove only the temporary files a store is known to write."""
    for temporary in directory.glob(".*.writing"):
        temporary.unlink(missing_ok=True)
    (directory / f"{CHECKPOINT_METADATA_NAME}.tmp").unlink(missing_ok=True)


def _part_path(directory: Path, index: int) -> Path:
    """Return the stable zero-padded path for one checkpoint part."""
    return directory / f"part-{index:08d}.parquet"


def _stream_parts(
    writer: pq.ParquetWriter,
    parts: tuple[Path, ...],
    *,
    batch_rows: int,
) -> tuple[int, int]:
    """Copy every durable part into a writer and return row-size metrics."""
    assembled_rows = 0
    max_batch_rows = 0
    for part in parts:
        parquet = pq.ParquetFile(part)
        for batch in parquet.iter_batches(batch_size=batch_rows):
            writer.write_batch(batch)
            assembled_rows += batch.num_rows
            max_batch_rows = max(max_batch_rows, batch.num_rows)
    return assembled_rows, max_batch_rows


__all__ = [
    "CHECKPOINT_METADATA_NAME",
    "CHECKPOINT_VERSION",
    "Checkpoint",
    "CheckpointStore",
]
