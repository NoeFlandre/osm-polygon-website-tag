"""Per-PBF extraction.

This module turns one source ``.osm.pbf`` into three deterministic
Parquet shards under a run-owned directory:

* ``polygons/<stem>.parquet`` -- :data:`POLYGON_PUBLIC_SCHEMA`.
  Website-qualified polygons (the public dataset).
* ``analysis_observations/<stem>.parquet`` -- :data:`COMPARISON_OBSERVATION_SCHEMA`.
  Compact comparison observations (``has_any_website OR has_wikidata``).
* ``rejections/<stem>.parquet`` -- :data:`REJECTION_SCHEMA`.
  Candidates whose geometry could not be assembled and other
  expected exclusions. Empty shards are still written.

Public surface
--------------

* :func:`extract_pbf` -- process one PBF and write all three shards.
* :class:`ExtractionResult` -- counts returned by :func:`extract_pbf`.
* :class:`ExtractFailure` -- a structured processing-failure record
  (NOT an expected exclusion).

Bounded processing
------------------

The handler accumulates up to ``FLUSH_BATCH_ROWS`` rows per shard in
memory and flushes them via ``pq.ParquetWriter`` before continuing.
Memory usage is bounded by ``FLUSH_BATCH_ROWS`` regardless of the
number of polygons in the PBF.

Candidate ledger
----------------

The extractor records every website- or Wikidata-qualified closed way and
supported polygon relation as a candidate. After ``apply_file`` finishes, candidates whose
``area()`` callback never fired are written to the rejection shard
with ``rejection_kind="no_area_callback"``. Duplicate ``area()``
callbacks for the same ``(osm_type, osm_id)`` are recorded with
``rejection_kind="duplicate_area_callback"``.

No full candidate set is held in memory; the on-disk candidate log is
streamed and reconciled with the area-callback log after ``apply_file``.

Row builders
------------

The public, comparison, and rejection row builders share a single normalized
tag projection (:func:`osm_polygon_website_tag.pipeline.record_builders.derive_tags`)
for the normalized `website` / `contact:website` values, the website presence
flags, and the primary category. The projection is computed once per builder
invocation (not once per OSM object): each builder that runs reads it
independently. The comparison and rejection builders additionally call
:func:`osm_polygon_website_tag.pipeline.record_builders.derive_wikidata` to
obtain the normalized `wikidata` value and its presence flag;
``_public_record`` does not call it because the public shard's v1.3 schema
omits Wikidata. URL classification and hostname extraction remain exclusive
to ``_public_record`` (the public shard) and are never applied to comparison
or rejection rows.

Source identity
---------------

No SHA-256 of the PBF is computed. Pre/post size and mtime equality
is the mutation gate (see :mod:`osm_polygon_website_tag.runtime.run_state`).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import osmium
import osmium.geom
import osmium.osm

from osm_polygon_website_tag.contracts.comparison_schema import (
    COMPARISON_OBSERVATION_SCHEMA,
    COMPARISON_OBSERVATION_SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    SCHEMA_VERSION,
    PublicRowInvariantError,
    validate_public_row,
)
from osm_polygon_website_tag.contracts.rejection_schema import (
    REJECTION_SCHEMA,
    REJECTION_SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields
from osm_polygon_website_tag.domain.geometry import GeometryRejection, geometry_from_area
from osm_polygon_website_tag.domain.region import region_from_pbf_filename
from osm_polygon_website_tag.domain.tags import (
    has_any_website,
    has_wikidata,
    normalize_value,
)
from osm_polygon_website_tag.domain.website import (
    classify_contact_website,
    classify_website,
    extract_contact_hostname,
    extract_hostname,
)
from osm_polygon_website_tag.pipeline.record_builders import derive_tags, derive_wikidata
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_INCOMPLETE,
    RunState,
    hash_shard,
    record_processed_source,
    snapshot_source_fingerprint,
    transition_status,
)
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink
from osm_polygon_website_tag.storage.candidate_ledger import CandidateLedger

# Flush a batch of polygons to the shard every N rows.
FLUSH_BATCH_ROWS = 5_000

# Closed-way inclusion requires the way to have at least this many
# distinct node references.
MIN_DISTINCT_NODES = 3


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


def _public_record(
    *,
    polygon_id: str,
    source_pbf: str,
    region: str,
    tags_dict: dict[str, str],
    osm_type: str,
    osm_id: int,
    osm_version: int,
    osm_timestamp: dt.datetime,
    geom_text: str,
    centroid_text: str,
    centroid_kind: str,
    lat: float,
    lon: float,
    bbox: list[float],
    area_m2: float,
    area_bucket: str,
) -> dict[str, object]:
    derived = derive_tags(tags_dict)
    name_raw = normalize_value(tags_dict.get("name", "")) or None
    website_class = classify_website(derived.website).value if derived.website else None
    contact_class = (
        classify_contact_website(derived.contact_website).value if derived.contact_website else None
    )
    website_hostname = extract_hostname(derived.website) if derived.website else None
    contact_hostname = (
        extract_contact_hostname(derived.contact_website) if derived.contact_website else None
    )

    tag_keys_sorted = sorted(tags_dict.keys())
    tags_json = json.dumps(tags_dict, sort_keys=True, separators=(",", ":"))
    tag_keys_json = json.dumps(tag_keys_sorted, separators=(",", ":"))
    bbox_json = json.dumps(bbox, separators=(",", ":"))

    record: dict[str, object] = {
        "polygon_id": polygon_id,
        "region": region,
        "source_pbf": source_pbf,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": osm_version,
        "osm_timestamp": osm_timestamp,
        "name": name_raw,
        "website": derived.website,
        "contact_website": derived.contact_website,
        "has_website": derived.has_website,
        "has_contact_website": derived.has_contact_website,
        "has_any_website": derived.has_any_website,
        "website_class": website_class,
        "contact_website_class": contact_class,
        "website_hostname": website_hostname,
        "contact_website_hostname": contact_hostname,
        "tags": tags_json,
        "tag_keys": tag_keys_json,
        "tag_count": len(tags_dict),
        "osm_primary_tag": derived.primary_category,
        "geometry": geom_text,
        "centroid": centroid_text,
        "centroid_kind": centroid_kind,
        "lat": lat,
        "lon": lon,
        "bbox": bbox_json,
        "area_m2": area_m2,
        "area_bucket": area_bucket,
        "schema_version": SCHEMA_VERSION,
    }
    record.update(
        initial_text_fields(
            website_present=derived.has_website,
            contact_website_present=derived.has_contact_website,
        )
    )
    validate_public_row(record)
    return record


def _comparison_record(
    *,
    source_pbf: str,
    region: str,
    tags_dict: dict[str, str],
    osm_type: str,
    osm_id: int,
    osm_version: int,
    osm_timestamp: dt.datetime,
) -> dict[str, object]:
    derived = derive_tags(tags_dict)
    wikidata, has_wikidata = derive_wikidata(tags_dict)
    return {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": osm_version,
        "osm_timestamp": osm_timestamp,
        "source_pbf": source_pbf,
        "region": region,
        "primary_category": derived.primary_category,
        "website": derived.website,
        "contact_website": derived.contact_website,
        "wikidata": wikidata,
        "has_website": derived.has_website,
        "has_contact_website": derived.has_contact_website,
        "has_any_website": derived.has_any_website,
        "has_wikidata": has_wikidata,
        "schema_version": COMPARISON_OBSERVATION_SCHEMA_VERSION,
    }


def _rejection_record(
    *,
    source_pbf: str,
    region: str,
    tags_dict: dict[str, str],
    osm_type: str,
    osm_id: int,
    osm_version: int,
    osm_timestamp: dt.datetime,
    candidate_kind: str,
    rejection_kind: str,
    message: str,
) -> dict[str, object]:
    derived = derive_tags(tags_dict)
    wikidata, has_wikidata = derive_wikidata(tags_dict)
    return {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": osm_version,
        "osm_timestamp": osm_timestamp,
        "source_pbf": source_pbf,
        "region": region,
        "primary_category": derived.primary_category,
        "website": derived.website,
        "contact_website": derived.contact_website,
        "wikidata": wikidata,
        "has_website": derived.has_website,
        "has_contact_website": derived.has_contact_website,
        "has_any_website": derived.has_any_website,
        "has_wikidata": has_wikidata,
        "candidate_kind": candidate_kind,
        "rejection_kind": rejection_kind,
        "message": message,
        "schema_version": REJECTION_SCHEMA_VERSION,
    }


class _ExtractionHandler(osmium.SimpleHandler):
    """Streamed per-PBF extractor.

    ``way`` records closed ways and, if they have website/wikidata,
    writes them to the candidate ledger. ``relation`` records supported
    polygon relations and writes them to the candidate ledger.
    ``area`` writes public rows, comparison observations, and
    rejections via bounded ParquetWriter batches. After ``apply_file``
    finishes, the handler reconciles the candidate ledger with the
    area-callback set: candidates whose area never fired go to the
    rejection shard with ``rejection_kind="no_area_callback"``.
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
    ) -> None:
        super().__init__()
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
        if has_any_website(tags_dict):
            # Public row attempt.
            try:
                geom = geometry_from_area(a)
            except GeometryRejection as rej:
                self._flush_geometry_rejection(a, rej.kind, rej.message)
                return
            except Exception as e:
                self._flush_geometry_rejection(a, "geometry_error", f"{type(e).__name__}: {e}")
                return
            polygon_id = f"{self._stem}:{osm_type}/{osm_id}"
            try:
                record = _public_record(
                    polygon_id=polygon_id,
                    source_pbf=self._source_pbf,
                    region=self._region,
                    tags_dict=tags_dict,
                    osm_type=osm_type,
                    osm_id=osm_id,
                    osm_version=int(a.version),
                    osm_timestamp=_as_utc(a.timestamp),
                    geom_text=geom.geometry,
                    centroid_text=geom.centroid,
                    centroid_kind=geom.centroid_kind,
                    lat=geom.lat,
                    lon=geom.lon,
                    bbox=geom.bbox,
                    area_m2=geom.area_m2,
                    area_bucket=geom.area_bucket,
                )
            except PublicRowInvariantError as inv:
                self._flush_geometry_rejection(a, "public_invariant_violation", str(inv))
                return
            self.public_sink.add(record)
        if has_any_website(tags_dict) or has_wikidata(tags_dict):
            self.obs_sink.add(
                _comparison_record(
                    source_pbf=self._source_pbf,
                    region=self._region,
                    tags_dict=tags_dict,
                    osm_type=osm_type,
                    osm_id=osm_id,
                    osm_version=int(a.version),
                    osm_timestamp=_as_utc(a.timestamp),
                )
            )

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
        """After ``apply_file``, write missing-area rejections and
        return counts."""
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
        self.public_sink.close()
        self.obs_sink.close()
        self.rej_sink.close()
        self.ledger.close()


def _as_utc(ts: Any) -> dt.datetime:
    result: dt.datetime = ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    return result


def extract_pbf(
    pbf_path: Path | str,
    run_dir: Path | str,
    run_state: RunState | None = None,
) -> ExtractionResult:
    """Extract one source PBF into three deterministic Parquet shards.

    ``pbf_path`` must point to a single ``.osm.pbf`` file. Passing a
    directory raises :class:`ValueError`. Each invocation writes:

    * ``<run_dir>/polygons/<stem>.parquet`` -- POLYGON_PUBLIC_SCHEMA
    * ``<run_dir>/analysis_observations/<stem>.parquet`` -- COMPARISON_OBSERVATION_SCHEMA
    * ``<run_dir>/rejections/<stem>.parquet`` -- REJECTION_SCHEMA

    The three shards are written atomically (temp + replace). Empty
    shards are schema-valid Parquet with zero rows. A genuine crash
    raises; expected exclusions are recorded in the rejection shard.
    """
    pbf_path = Path(pbf_path)
    run_dir = Path(run_dir)
    if pbf_path.is_dir():
        raise ValueError(f"extract_pbf requires a .osm.pbf file path; got directory {pbf_path}")
    if not pbf_path.is_file():
        raise FileNotFoundError(pbf_path)
    if pbf_path.suffix != ".pbf" or not pbf_path.name.endswith(".osm.pbf"):
        raise ValueError(f"not a .osm.pbf file: {pbf_path}")

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
        handler.close()
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
    "FLUSH_BATCH_ROWS",
    "ExtractFailure",
    "ExtractionResult",
    "extract_pbf",
]
