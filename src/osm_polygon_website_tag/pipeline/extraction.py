"""Per-PBF extraction orchestration.

This module validates one source PBF, owns run-state transitions, promotes
the three staged shards atomically, and returns the extraction result. The
libosmium callbacks and bounded area-handler state live in
:mod:`osm_polygon_website_tag.pipeline.extraction_handler` so the two
responsibilities can evolve and be tested independently.

The output contract is unchanged:

* ``polygons/<stem>.parquet`` -- website-qualified public polygons;
* ``analysis_observations/<stem>.parquet`` -- website/Wikidata observations;
* ``rejections/<stem>.parquet`` -- expected exclusions and geometry failures.

No SHA-256 of the PBF is computed. Pre/post size and mtime equality is the
mutation gate (see :mod:`osm_polygon_website_tag.runtime.run_state`).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from osm_polygon_website_tag.domain.region import region_from_pbf_filename
from osm_polygon_website_tag.pipeline import extraction_handler as _extraction_handler
from osm_polygon_website_tag.pipeline import extraction_records as _extraction_records
from osm_polygon_website_tag.pipeline.area_work import (
    DEFAULT_AREA_WORKERS,
    DEFAULT_MAX_IN_FLIGHT_AREAS,
    MAX_AREA_WORKERS,
    MAX_IN_FLIGHT_AREAS,
    AreaPayload,
    AreaResult,
)
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_INCOMPLETE,
    RunState,
    hash_shard,
    record_processed_source,
    snapshot_source_fingerprint,
    transition_status,
)
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle

# Re-export the handler's constants and implementation names so established
# imports from this module continue to work while the implementation has a
# focused home.
FLUSH_BATCH_ROWS = _extraction_handler.FLUSH_BATCH_ROWS
MIN_DISTINCT_NODES = _extraction_handler.MIN_DISTINCT_NODES
_ExtractionHandler = _extraction_handler._ExtractionHandler
_AreaWorkCoordinator = _extraction_handler._AreaWorkCoordinator
_validate_area_settings = _extraction_handler._validate_area_settings
_process_area_payload = _extraction_handler._process_area_payload
_is_closed_way = _extraction_handler._is_closed_way
_is_supported_polygon_relation = _extraction_handler._is_supported_polygon_relation
_tags_dict = _extraction_handler._tags_dict
_as_utc = _extraction_handler._as_utc
derive_tags = _extraction_handler.derive_tags

# Preserve the established private row-builder facade.
_public_record = _extraction_records.build_public_record
_comparison_record = _extraction_records.build_comparison_record
_rejection_record = _extraction_records.build_rejection_record


@dataclass(frozen=True)
class ExtractFailure:
    """A structured processing-failure record produced by :func:`extract_pbf`.

    A processing failure is a genuine crash (extractor bug, I/O error,
    unexpected exception). It transitions the run to ``INCOMPLETE``.
    Expected exclusions go to the rejection shard, not to this record.
    """

    source_pbf: str
    osm_type: str
    osm_id: int
    phase: str
    kind: str
    message: str
    timestamp: str


@dataclass(frozen=True)
class ExtractionResult:
    """The result of extracting one source PBF."""

    source_pbf: str
    region: str
    public_row_count: int
    observation_row_count: int
    rejection_count: int
    duration_seconds: float
    started_at: str
    finished_at: str


def _now_iso() -> str:
    return dt.datetime.now(tz=dt.UTC).replace(microsecond=0).isoformat()


def extract_pbf(
    pbf_path: Path | str,
    run_dir: Path | str,
    run_state: RunState | None = None,
    *,
    area_workers: int = DEFAULT_AREA_WORKERS,
    max_in_flight_areas: int = DEFAULT_MAX_IN_FLIGHT_AREAS,
) -> ExtractionResult:
    """Extract one source PBF into three deterministic Parquet shards.

    ``pbf_path`` must point to a single ``.osm.pbf`` file. Passing a
    directory raises :class:`ValueError`. Each invocation writes:

    * ``<run_dir>/polygons/<stem>.parquet`` -- POLYGON_PUBLIC_SCHEMA
    * ``<run_dir>/analysis_observations/<stem>.parquet`` -- COMPARISON_OBSERVATION_SCHEMA
    * ``<run_dir>/rejections/<stem>.parquet`` -- REJECTION_SCHEMA

    ``area_workers`` controls bounded pure geometry/row construction work;
    ``max_in_flight_areas`` limits queued payloads and preserves bounded
    memory. Results are drained in callback order, so changing either value
    does not change shard rows or hashes. The callback thread retains
    ownership of libosmium, SQLite, and Parquet state.

    The three shards are written atomically (temp + replace). Empty shards
    are schema-valid Parquet with zero rows. A genuine crash raises;
    expected exclusions are recorded in the rejection shard.
    """
    pbf_path = Path(pbf_path)
    run_dir = Path(run_dir)
    if pbf_path.is_dir():
        raise ValueError(f"extract_pbf requires a .osm.pbf file path; got directory {pbf_path}")
    if not pbf_path.is_file():
        raise FileNotFoundError(pbf_path)
    if pbf_path.suffix != ".pbf" or not pbf_path.name.endswith(".osm.pbf"):
        raise ValueError(f"not a .osm.pbf file: {pbf_path}")
    _validate_area_settings(area_workers, max_in_flight_areas)

    started = dt.datetime.now(tz=dt.UTC)
    started_iso = started.replace(microsecond=0).isoformat()
    source_before = snapshot_source_fingerprint(pbf_path)

    stem = pbf_path.name.removesuffix(".osm.pbf")
    region = region_from_pbf_filename(pbf_path.name)

    polygons_dir = run_dir / "polygons"
    obs_dir = run_dir / "analysis_observations"
    rej_dir = run_dir / "rejections"

    handler = _ExtractionHandler(
        source_pbf=pbf_path.name,
        region=region,
        stem=stem,
        polygons_dir=polygons_dir,
        obs_dir=obs_dir,
        rej_dir=rej_dir,
        area_workers=area_workers,
        max_in_flight_areas=max_in_flight_areas,
    )
    public_final = polygons_dir / f"{stem}.parquet"
    obs_final = obs_dir / f"{stem}.parquet"
    rej_final = rej_dir / f"{stem}.parquet"
    staged_paths = (
        handler.public_sink.path,
        handler.obs_sink.path,
        handler.rej_sink.path,
        handler.ledger.path,
    )
    try:
        handler.apply_file(str(pbf_path))
        handler.reconcile_candidates()
        handler.close()
        source_after = snapshot_source_fingerprint(pbf_path)
        if source_after != source_before:
            raise RuntimeError(f"source changed during extraction: {pbf_path.name}")

        public_count = handler.public_sink.row_count
        obs_count = handler.obs_sink.row_count
        rej_count = handler.rej_sink.row_count
        atomic_promote_bundle(
            [
                (handler.public_sink.path, public_final),
                (handler.obs_sink.path, obs_final),
                (handler.rej_sink.path, rej_final),
            ]
        )
    except BaseException as exc:
        handler.abort()
        for path in staged_paths:
            path.unlink(missing_ok=True)
        if run_state is not None and isinstance(exc, Exception):
            failure = ExtractFailure(
                source_pbf=pbf_path.name,
                osm_type="",
                osm_id=0,
                phase="extract",
                kind=type(exc).__name__,
                message=str(exc).replace(str(pbf_path), pbf_path.name),
                timestamp=_now_iso(),
            )
            with (run_dir / "failures.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(failure), sort_keys=True) + "\n")
            transition_status(run_state, STATUS_INCOMPLETE)
        raise
    handler.ledger.path.unlink(missing_ok=True)

    finished = dt.datetime.now(tz=dt.UTC)
    finished_iso = finished.replace(microsecond=0).isoformat()
    duration = (finished - started).total_seconds()

    if run_state is not None:
        public_sha = hash_shard(public_final) if public_final.exists() else None
        obs_sha = hash_shard(obs_final) if obs_final.exists() else None
        rej_sha = hash_shard(rej_final) if rej_final.exists() else None
        record_processed_source(
            run_state,
            source_after,
            public_row_count=public_count,
            observation_row_count=obs_count,
            rejection_count=rej_count,
            started_at=started_iso,
            finished_at=finished_iso,
            public_shard_sha256=public_sha,
            observation_shard_sha256=obs_sha,
            rejection_shard_sha256=rej_sha,
            status="extracted",
        )

    return ExtractionResult(
        source_pbf=pbf_path.name,
        region=region,
        public_row_count=public_count,
        observation_row_count=obs_count,
        rejection_count=rej_count,
        duration_seconds=duration,
        started_at=started_iso,
        finished_at=finished_iso,
    )


__all__ = [
    "DEFAULT_AREA_WORKERS",
    "DEFAULT_MAX_IN_FLIGHT_AREAS",
    "FLUSH_BATCH_ROWS",
    "MAX_AREA_WORKERS",
    "MAX_IN_FLIGHT_AREAS",
    "AreaPayload",
    "AreaResult",
    "ExtractFailure",
    "ExtractionResult",
    "extract_pbf",
]
