"""libosmium callbacks and bounded area processing for one PBF.

This module owns the callback-side extraction state: candidate recording,
area-work coordination, bounded Parquet sinks, and candidate reconciliation.
The public ``extract_pbf`` orchestration and run-state updates remain in
:mod:`osm_polygon_website_tag.pipeline.extraction`.

The callback thread retains ownership of libosmium, SQLite, and Parquet
state. Pure geometry and row construction may run through the bounded area
coordinator, but results are emitted in callback order.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, cast

import osmium
import osmium.geom
import osmium.osm

from osm_polygon_website_tag.contracts.comparison_schema import (
    COMPARISON_OBSERVATION_SCHEMA,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    PublicRowInvariantError,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.domain.geometry import GeometryRejection, geometry_from_geojson
from osm_polygon_website_tag.domain.tags import has_any_website, has_wikidata
from osm_polygon_website_tag.pipeline import extraction_records as _extraction_records
from osm_polygon_website_tag.pipeline.area_work import (
    DEFAULT_AREA_WORKERS,
    DEFAULT_MAX_IN_FLIGHT_AREAS,
    AreaPayload,
    AreaResult,
)
from osm_polygon_website_tag.pipeline.area_work import (
    AreaWorkCoordinator as _AreaWorkCoordinator,
)
from osm_polygon_website_tag.pipeline.area_work import (
    validate_area_settings as _validate_area_settings,
)
from osm_polygon_website_tag.pipeline.record_builders import derive_tags
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink
from osm_polygon_website_tag.storage.candidate_ledger import CandidateLedger

# Flush a batch of polygons to the shard every N rows.
FLUSH_BATCH_ROWS = 5_000

# Closed-way inclusion requires the way to have at least this many
# distinct node references.
MIN_DISTINCT_NODES = 3

# Keep the established row-builder aliases local to the callback implementation.
_public_record = _extraction_records.build_public_record
_comparison_record = _extraction_records.build_comparison_record
_rejection_record = _extraction_records.build_rejection_record


def _process_area_payload(payload: AreaPayload) -> AreaResult:
    """Build one area result without accessing libosmium or shared state."""
    tags_dict = payload.tags_dict
    derived = payload.derived_tags
    if derived is None:
        derived = derive_tags(tags_dict)
    if derived.has_any_website:
        if payload.raw_geojson is None:
            return AreaResult(
                rejection_row=_rejection_record(
                    source_pbf=payload.source_pbf,
                    region=payload.region,
                    tags_dict=tags_dict,
                    osm_type=payload.osm_type,
                    osm_id=payload.osm_id,
                    osm_version=payload.osm_version,
                    osm_timestamp=payload.osm_timestamp,
                    candidate_kind=payload.candidate_kind,
                    rejection_kind="geometry_error",
                    message="missing serialized area geometry",
                    derived=derived,
                )
            )
        try:
            geom = geometry_from_geojson(payload.raw_geojson)
        except GeometryRejection as rej:
            return AreaResult(
                rejection_row=_rejection_record(
                    source_pbf=payload.source_pbf,
                    region=payload.region,
                    tags_dict=tags_dict,
                    osm_type=payload.osm_type,
                    osm_id=payload.osm_id,
                    osm_version=payload.osm_version,
                    osm_timestamp=payload.osm_timestamp,
                    candidate_kind=payload.candidate_kind,
                    rejection_kind=rej.kind,
                    message=rej.message,
                    derived=derived,
                )
            )
        except Exception as exc:
            return AreaResult(
                rejection_row=_rejection_record(
                    source_pbf=payload.source_pbf,
                    region=payload.region,
                    tags_dict=tags_dict,
                    osm_type=payload.osm_type,
                    osm_id=payload.osm_id,
                    osm_version=payload.osm_version,
                    osm_timestamp=payload.osm_timestamp,
                    candidate_kind=payload.candidate_kind,
                    rejection_kind="geometry_error",
                    message=f"{type(exc).__name__}: {exc}",
                    derived=derived,
                )
            )
        stem = payload.source_pbf.removesuffix(".osm.pbf")
        polygon_id = f"{stem}:{payload.osm_type}/{payload.osm_id}"
        try:
            public_row = _public_record(
                polygon_id=polygon_id,
                source_pbf=payload.source_pbf,
                region=payload.region,
                tags_dict=tags_dict,
                osm_type=payload.osm_type,
                osm_id=payload.osm_id,
                osm_version=payload.osm_version,
                osm_timestamp=payload.osm_timestamp,
                geom_text=geom.geometry,
                centroid_text=geom.centroid,
                centroid_kind=geom.centroid_kind,
                lat=geom.lat,
                lon=geom.lon,
                bbox=geom.bbox,
                area_m2=geom.area_m2,
                area_bucket=geom.area_bucket,
                derived=derived,
            )
        except PublicRowInvariantError as inv:
            return AreaResult(
                rejection_row=_rejection_record(
                    source_pbf=payload.source_pbf,
                    region=payload.region,
                    tags_dict=tags_dict,
                    osm_type=payload.osm_type,
                    osm_id=payload.osm_id,
                    osm_version=payload.osm_version,
                    osm_timestamp=payload.osm_timestamp,
                    candidate_kind=payload.candidate_kind,
                    rejection_kind="public_invariant_violation",
                    message=str(inv),
                    derived=derived,
                )
            )
    else:
        public_row = None

    observation_row = _comparison_record(
        source_pbf=payload.source_pbf,
        region=payload.region,
        tags_dict=tags_dict,
        osm_type=payload.osm_type,
        osm_id=payload.osm_id,
        osm_version=payload.osm_version,
        osm_timestamp=payload.osm_timestamp,
        derived=derived,
    )
    return AreaResult(public_row=public_row, observation_row=observation_row)


def _is_closed_way(way: osmium.osm.Way) -> bool:
    nodes = [n.ref for n in way.nodes]
    if len(nodes) < MIN_DISTINCT_NODES + 1:
        return False
    if nodes[0] != nodes[-1]:
        return False
    return not len(set(nodes[:-1])) < MIN_DISTINCT_NODES


def _is_supported_polygon_relation(relation: osmium.osm.Relation) -> bool:
    for k, v in relation.tags:  # noqa: SIM110
        if k == "type" and v in ("multipolygon", "boundary"):
            return True
    return False


def _tags_dict(obj: osmium.osm.Area | osmium.osm.Way | osmium.osm.Relation) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in obj.tags:
        out[k] = v
    return out


class _ExtractionHandler(osmium.SimpleHandler):
    """Streamed per-PBF extractor.

    ``way`` records closed ways and, if they have website/wikidata,
    writes them to the candidate ledger. ``relation`` records supported
    polygon relations and writes them to the candidate ledger. ``area``
    writes public rows, comparison observations, and rejections via bounded
    ParquetWriter batches. After ``apply_file`` finishes, the handler
    reconciles the candidate ledger with the area-callback set: candidates
    whose area never fired go to the rejection shard with
    ``rejection_kind="no_area_callback"``.
    """

    def __init__(
        self,
        *,
        source_pbf: str,
        region: str,
        stem: str,
        polygons_dir: Path,
        obs_dir: Path,
        rej_dir: Path,
        area_workers: int = DEFAULT_AREA_WORKERS,
        max_in_flight_areas: int = DEFAULT_MAX_IN_FLIGHT_AREAS,
    ) -> None:
        super().__init__()
        _validate_area_settings(area_workers, max_in_flight_areas)
        self._source_pbf = source_pbf
        self._region = region
        self._stem = stem
        self._polygons_dir = polygons_dir
        self._obs_dir = obs_dir
        self._rej_dir = rej_dir
        self.public_sink = BatchParquetSink(
            polygons_dir / f".{stem}.public.parquet",
            POLYGON_PUBLIC_SCHEMA,
            batch_rows=FLUSH_BATCH_ROWS,
        )
        self.obs_sink = BatchParquetSink(
            obs_dir / f".{stem}.observations.parquet",
            COMPARISON_OBSERVATION_SCHEMA,
            batch_rows=FLUSH_BATCH_ROWS,
        )
        self.rej_sink = BatchParquetSink(
            rej_dir / f".{stem}.rejections.parquet",
            REJECTION_SCHEMA,
            batch_rows=FLUSH_BATCH_ROWS,
        )
        self.ledger = CandidateLedger(rej_dir / f".{stem}.candidates.sqlite3")
        self._area_sequence = 0
        self._area_coordinator = _AreaWorkCoordinator(
            area_workers=area_workers,
            max_in_flight_areas=max_in_flight_areas,
            processor=_process_area_payload,
        )

    def _emit_area_result(self, result: AreaResult) -> None:
        if result.public_row is not None:
            self.public_sink.add(result.public_row)
        if result.observation_row is not None:
            self.obs_sink.add(result.observation_row)
        if result.rejection_row is not None:
            self.rej_sink.add(result.rejection_row)

    def _drain_area_work(self) -> None:
        for result in self._area_coordinator.drain():
            self._emit_area_result(result)

    def _record_candidate(
        self,
        osm_type: str,
        osm_id: int,
        tags_dict: dict[str, str],
        osm_version: int,
        osm_timestamp: dt.datetime,
        candidate_kind: str,
    ) -> None:
        self.ledger.upsert(
            osm_type,
            osm_id,
            tags_dict,
            osm_version,
            osm_timestamp,
            candidate_kind,
        )

    def _flush_geometry_rejection(
        self,
        area: osmium.osm.Area,
        kind: str,
        message: str,
    ) -> None:
        tags_dict = _tags_dict(area)
        osm_type = "relation" if area.from_way() is False else "way"
        self.rej_sink.add(
            _rejection_record(
                source_pbf=self._source_pbf,
                region=self._region,
                tags_dict=tags_dict,
                osm_type=osm_type,
                osm_id=int(area.orig_id()),
                osm_version=int(area.version),
                osm_timestamp=_as_utc(area.timestamp),
                candidate_kind="closed_way" if osm_type == "way" else "relation_polygon",
                rejection_kind=kind,
                message=message,
            )
        )

    def area(self, a: osmium.osm.Area) -> None:
        osm_type = "relation" if a.from_way() is False else "way"
        osm_id = int(a.orig_id())
        try:
            tracked = self.ledger.mark_area_seen(osm_type, osm_id)
        except ValueError:
            self._drain_area_work()
            self.rej_sink.add(
                _rejection_record(
                    source_pbf=self._source_pbf,
                    region=self._region,
                    tags_dict=_tags_dict(a),
                    osm_type=osm_type,
                    osm_id=osm_id,
                    osm_version=int(a.version),
                    osm_timestamp=_as_utc(a.timestamp),
                    candidate_kind="closed_way" if osm_type == "way" else "relation_polygon",
                    rejection_kind="duplicate_area_callback",
                    message="area callback fired more than once for the same object",
                )
            )
            return
        candidate = self.ledger.get(osm_type, osm_id) if tracked else None
        if candidate is None:
            # Unexpected: an area() fired for an object we never saw in
            # way()/relation(). Could happen if a relation uses
            # sub-relations we did not pre-record. We still treat it
            # as a candidate and emit a rejection for the
            # not-tracked reason.
            tags_dict = _tags_dict(a)
            self._drain_area_work()
            self.rej_sink.add(
                _rejection_record(
                    source_pbf=self._source_pbf,
                    region=self._region,
                    tags_dict=tags_dict,
                    osm_type=osm_type,
                    osm_id=osm_id,
                    osm_version=int(a.version),
                    osm_timestamp=_as_utc(a.timestamp),
                    candidate_kind="closed_way" if osm_type == "way" else "relation_polygon",
                    rejection_kind="untracked_candidate",
                    message="area callback fired without a prior candidate record",
                )
            )
            return
        tags_dict = cast(dict[str, str], candidate["tags"])
        derived = derive_tags(tags_dict)
        has_website = derived.has_any_website
        has_candidate_observation = has_website or has_wikidata(tags_dict)
        if has_candidate_observation:
            raw_geojson: str | None = None
            if has_website:
                try:
                    raw_geojson = osmium.geom.GeoJSONFactory().create_multipolygon(a)
                except GeometryRejection as rej:
                    self._drain_area_work()
                    self._flush_geometry_rejection(a, rej.kind, rej.message)
                    return
                except Exception as exc:
                    self._drain_area_work()
                    self._flush_geometry_rejection(
                        a, "geometry_error", f"{type(exc).__name__}: {exc}"
                    )
                    return

            payload = AreaPayload(
                sequence=self._area_sequence,
                source_pbf=self._source_pbf,
                region=self._region,
                tags_dict=dict(tags_dict),
                osm_type=osm_type,
                osm_id=osm_id,
                osm_version=int(a.version),
                osm_timestamp=_as_utc(a.timestamp),
                candidate_kind=str(candidate["candidate_kind"]),
                raw_geojson=raw_geojson,
                derived_tags=derived,
            )
            self._area_sequence += 1
            result = self._area_coordinator.submit(payload)
            if result is not None:
                self._emit_area_result(result)

    def way(self, w: osmium.osm.Way) -> None:
        tags_dict = _tags_dict(w)
        if has_any_website(tags_dict) or has_wikidata(tags_dict):
            if not _is_closed_way(w):
                # Open way with a qualifying tag: it's an expected
                # exclusion -- it can never be a polygon.
                self.rej_sink.add(
                    _rejection_record(
                        source_pbf=self._source_pbf,
                        region=self._region,
                        tags_dict=tags_dict,
                        osm_type="way",
                        osm_id=int(w.id),
                        osm_version=int(w.version),
                        osm_timestamp=_as_utc(w.timestamp),
                        candidate_kind="closed_way",
                        rejection_kind="open_way_with_website",
                        message="open way with a qualifying website/wikidata tag",
                    )
                )
                return
            self._record_candidate(
                "way",
                int(w.id),
                tags_dict,
                int(w.version),
                _as_utc(w.timestamp),
                "closed_way",
            )

    def relation(self, r: osmium.osm.Relation) -> None:
        tags_dict = _tags_dict(r)
        if not _is_supported_polygon_relation(r):
            return
        if has_any_website(tags_dict) or has_wikidata(tags_dict):
            self._record_candidate(
                "relation",
                int(r.id),
                tags_dict,
                int(r.version),
                _as_utc(r.timestamp),
                "relation_polygon",
            )

    def reconcile_candidates(self) -> None:
        """After ``apply_file``, write missing-area rejections."""
        # Area results must be emitted before reconciliation appends
        # ``no_area_callback`` rows, matching the sequential callback order.
        self._drain_area_work()
        for osm_type, osm_id, candidate in self.ledger.missing_areas():
            tags_dict = cast(dict[str, str], candidate["tags"])
            self.rej_sink.add(
                _rejection_record(
                    source_pbf=self._source_pbf,
                    region=self._region,
                    tags_dict=tags_dict,
                    osm_type=osm_type,
                    osm_id=osm_id,
                    osm_version=int(candidate["osm_version"]),
                    osm_timestamp=cast(dt.datetime, candidate["osm_timestamp"]),
                    candidate_kind=str(candidate["candidate_kind"]),
                    rejection_kind="no_area_callback",
                    message="candidate was recorded but area() callback never fired",
                )
            )

    def close(self) -> None:
        """Drain ordered worker results, then close all per-source sinks."""
        try:
            for result in self._area_coordinator.close():
                self._emit_area_result(result)
        finally:
            self.public_sink.close()
            self.obs_sink.close()
            self.rej_sink.close()
            self.ledger.close()

    def abort(self) -> None:
        """Cancel queued area work and close partial sinks after a failure."""
        try:
            self._area_coordinator.abort()
        finally:
            self.public_sink.close()
            self.obs_sink.close()
            self.rej_sink.close()
            self.ledger.close()


def _as_utc(ts: Any) -> dt.datetime:
    result: dt.datetime = ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    return result


__all__ = [
    "DEFAULT_AREA_WORKERS",
    "DEFAULT_MAX_IN_FLIGHT_AREAS",
    "FLUSH_BATCH_ROWS",
    "MIN_DISTINCT_NODES",
    "AreaPayload",
    "AreaResult",
]
