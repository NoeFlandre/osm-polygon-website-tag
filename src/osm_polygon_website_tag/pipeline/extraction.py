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
    SourceFingerprint,
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
    _validate_pbf_path(pbf_path)
    _validate_area_settings(area_workers, max_in_flight_areas)

    started = dt.datetime.now(tz=dt.UTC)
    started_iso = started.replace(microsecond=0).isoformat()
    source_before = snapshot_source_fingerprint(pbf_path)
    stem = pbf_path.name.removesuffix(".osm.pbf")
    region = region_from_pbf_filename(pbf_path.name)
    handler, final_paths = _prepare_extraction(
        pbf_path,
        run_dir,
        region=region,
        stem=stem,
        area_workers=area_workers,
        max_in_flight_areas=max_in_flight_areas,
    )
    staged_paths = _staged_paths(handler)
    try:
        source_after, counts = _extract_and_promote(
            handler, pbf_path, source_before=source_before, final_paths=final_paths
        )
    except BaseException as exc:
        _abort_extraction(
            handler,
            staged_paths,
            pbf_path=pbf_path,
            run_dir=run_dir,
            run_state=run_state,
            error=exc,
        )
        raise
    handler.ledger.path.unlink(missing_ok=True)

    finished = dt.datetime.now(tz=dt.UTC)
    finished_iso = finished.replace(microsecond=0).isoformat()
    duration = (finished - started).total_seconds()
    public_count, obs_count, rej_count = counts

    if run_state is not None:
        _record_extraction_state(
            run_state,
            source_after,
            final_paths=final_paths,
            counts=counts,
            started_at=started_iso,
            finished_at=finished_iso,
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


def _validate_pbf_path(path: Path) -> None:
    """Validate the single-source input contract before opening outputs."""
    if path.is_dir():
        raise ValueError(f"extract_pbf requires a .osm.pbf file path; got directory {path}")
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix != ".pbf" or not path.name.endswith(".osm.pbf"):
        raise ValueError(f"not a .osm.pbf file: {path}")


def _prepare_extraction(
    pbf_path: Path,
    run_dir: Path,
    *,
    region: str,
    stem: str,
    area_workers: int,
    max_in_flight_areas: int,
) -> tuple[_ExtractionHandler, tuple[Path, Path, Path]]:
    """Create the callback handler and its three final shard destinations."""
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
    return handler, (
        polygons_dir / f"{stem}.parquet",
        obs_dir / f"{stem}.parquet",
        rej_dir / f"{stem}.parquet",
    )


def _staged_paths(handler: _ExtractionHandler) -> tuple[Path, ...]:
    """Return every scratch artifact that must be removed after a failure."""
    return (
        handler.public_sink.path,
        handler.obs_sink.path,
        handler.rej_sink.path,
        handler.ledger.path,
    )


def _extract_and_promote(
    handler: _ExtractionHandler,
    pbf_path: Path,
    *,
    source_before: SourceFingerprint,
    final_paths: tuple[Path, Path, Path],
) -> tuple[SourceFingerprint, tuple[int, int, int]]:
    """Run callbacks, enforce source immutability, and promote three shards."""
    handler.apply_file(str(pbf_path))
    handler.reconcile_candidates()
    handler.close()
    source_after = snapshot_source_fingerprint(pbf_path)
    if source_after != source_before:
        raise RuntimeError(f"source changed during extraction: {pbf_path.name}")
    counts = (handler.public_sink.row_count, handler.obs_sink.row_count, handler.rej_sink.row_count)
    atomic_promote_bundle(
        list(
            zip(
                (handler.public_sink.path, handler.obs_sink.path, handler.rej_sink.path),
                final_paths,
                strict=True,
            )
        )
    )
    return source_after, counts


def _abort_extraction(
    handler: _ExtractionHandler,
    staged_paths: tuple[Path, ...],
    *,
    pbf_path: Path,
    run_dir: Path,
    run_state: RunState | None,
    error: BaseException,
) -> None:
    """Close and remove scratch state, then persist an ordinary failure."""
    handler.abort()
    for path in staged_paths:
        path.unlink(missing_ok=True)
    if run_state is not None and isinstance(error, Exception):
        _write_extraction_failure(run_dir, pbf_path, run_state, error)


def _write_extraction_failure(
    run_dir: Path, pbf_path: Path, run_state: RunState, error: Exception
) -> None:
    """Append a sanitised failure record and transition the run to incomplete."""
    failure = ExtractFailure(
        source_pbf=pbf_path.name,
        osm_type="",
        osm_id=0,
        phase="extract",
        kind=type(error).__name__,
        message=str(error).replace(str(pbf_path), pbf_path.name),
        timestamp=_now_iso(),
    )
    with (run_dir / "failures.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(failure), sort_keys=True) + "\n")
    transition_status(run_state, STATUS_INCOMPLETE)


def _record_extraction_state(
    run_state: RunState,
    source_after: SourceFingerprint,
    *,
    final_paths: tuple[Path, Path, Path],
    counts: tuple[int, int, int],
    started_at: str,
    finished_at: str,
) -> None:
    """Persist source completion metadata and final shard fingerprints."""
    public_count, observation_count, rejection_count = counts
    public_path, observation_path, rejection_path = final_paths
    record_processed_source(
        run_state,
        source_after,
        public_row_count=public_count,
        observation_row_count=observation_count,
        rejection_count=rejection_count,
        started_at=started_at,
        finished_at=finished_at,
        public_shard_sha256=hash_shard(public_path) if public_path.exists() else None,
        observation_shard_sha256=hash_shard(observation_path)
        if observation_path.exists()
        else None,
        rejection_shard_sha256=hash_shard(rejection_path) if rejection_path.exists() else None,
        status="extracted",
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
