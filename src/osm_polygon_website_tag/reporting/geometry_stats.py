"""Deterministic polygon geometry statistics derived from validated artifacts.

Every value is computed from the validated public ``polygons/*.parquet``
shards of a run -- every valid row, with no sampling, truncation, or external
lookup. Nothing is read from raw PBFs, the network, or hand-written values.

The complete machine-readable result is written to ``<run_dir>/stats.json`` by
:func:`osm_polygon_website_tag.reporting.card.build_card`; the dataset card
renders a concise headline block from the same in-memory result, so the card
and ``stats.json`` can never disagree.

Determinism
-----------

* Shards are read in sorted order and rows in stored order.
* Totals use :func:`math.fsum`, which is exact and order-independent.
* Percentiles are exact nearest-rank values over every row.
* Every reported float is rounded to :data:`ROUNDING_DECIMALS` decimals, so
  unchanged input produces byte-identical output.

Memory
------

Geometry strings are decoded one record batch at a time and never retained;
only the compact numeric distributions (eight bytes per row and column) and
small counters survive a batch.
"""

from __future__ import annotations

import json
import math
from array import array
from bisect import bisect_right
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pyproj

GEOMETRY_STATS_FILENAME = "stats.json"
GEOMETRY_STATS_SCHEMA_VERSION = "v1"

#: Public polygon columns the computation reads; no other column is touched.
GEOMETRY_STATS_COLUMNS: tuple[str, ...] = ("geometry", "bbox", "area_m2", "osm_primary_tag")

#: Rows decoded per record batch, bounding peak geometry-string memory.
BATCH_ROWS = 8_192

#: Decimal places every reported float is rounded to.
ROUNDING_DECIMALS = 6

#: Reported percentiles, including the median at ``p50``.
PERCENTILES: tuple[int, ...] = (1, 5, 25, 50, 75, 95, 99)

#: Areas strictly below this many square metres are counted as suspiciously small.
TINY_AREA_M2 = 1.0

#: Absolute latitude at or beyond which a bounding box is counted as polar.
POLAR_LATITUDE_DEGREES = 85.0

#: Longitude span above which a bounding box is counted as antimeridian-crossing.
ANTIMERIDIAN_SPAN_DEGREES = 180.0

_GEOD = pyproj.Geod(ellps="WGS84")


#: Stable log-scale area buckets, smallest first. ``"0"`` holds exactly zero.
AREA_BUCKET_LABELS: tuple[str, ...] = (
    "0",
    "<1e0",
    *(f"1e{exponent}-1e{exponent + 1}" for exponent in range(10)),
    ">=1e10",
)

_AREA_BUCKET_EDGES: tuple[float, ...] = tuple(10.0**exponent for exponent in range(11))


@dataclass(frozen=True)
class NumericSummary:
    """Exact summary of one numeric distribution over every selected row."""

    row_count: int = 0
    total: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0
    mean: float = 0.0
    median: float = 0.0
    percentiles: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AreaStats:
    """Geodesic surface statistics over ``area_m2``."""

    summary: NumericSummary = field(default_factory=NumericSummary)
    histogram: list[dict[str, Any]] = field(default_factory=list)
    zero_area_row_count: int = 0
    below_one_m2_row_count: int = 0


@dataclass(frozen=True)
class ShapeStats:
    """Polygon/MultiPolygon composition and ring/vertex distributions."""

    polygon_row_count: int = 0
    multipolygon_row_count: int = 0
    with_holes_row_count: int = 0
    hole_ring_count: int = 0
    vertices_per_row: NumericSummary = field(default_factory=NumericSummary)
    rings_per_row: NumericSummary = field(default_factory=NumericSummary)
    components_per_multipolygon: NumericSummary = field(default_factory=NumericSummary)


@dataclass(frozen=True)
class ExtentStats:
    """Dataset extent and per-row bounding-box dimensions."""

    bbox: list[float] | None = None
    width_degrees: NumericSummary = field(default_factory=NumericSummary)
    height_degrees: NumericSummary = field(default_factory=NumericSummary)
    width_m: NumericSummary = field(default_factory=NumericSummary)
    height_m: NumericSummary = field(default_factory=NumericSummary)
    antimeridian_row_count: int = 0
    polar_row_count: int = 0


@dataclass(frozen=True)
class SourceAreaStats:
    """Per-source-PBF surface breakdown."""

    source_pbf: str
    row_count: int
    area_m2: NumericSummary


@dataclass(frozen=True)
class PrimaryTagAreaStats:
    """Per-primary-OSM-tag surface breakdown."""

    osm_primary_tag: str
    row_count: int
    total_area_m2: float


@dataclass(frozen=True)
class GeometryStats:
    """Complete, machine-readable polygon geometry statistics."""

    schema_version: str = GEOMETRY_STATS_SCHEMA_VERSION
    row_count: int = 0
    area: AreaStats = field(default_factory=AreaStats)
    shape: ShapeStats = field(default_factory=ShapeStats)
    extent: ExtentStats = field(default_factory=ExtentStats)
    per_source: list[SourceAreaStats] = field(default_factory=list)
    per_osm_primary_tag: list[PrimaryTagAreaStats] = field(default_factory=list)


def compute_geometry_stats(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> GeometryStats:
    """Compute geometry statistics from every selected public polygon row.

    Raises :class:`FileNotFoundError` when ``polygons/`` is missing and
    :class:`ValueError` when a selected shard lacks a required column.
    """
    root = Path(run_dir)
    directory = root / "polygons"
    if not directory.is_dir():
        raise FileNotFoundError(f"missing {directory}")
    accumulator = _Accumulator()
    per_source: list[SourceAreaStats] = []
    for shard in _selected_shards(directory, source_names):
        start = len(accumulator.areas)
        _accumulate_shard(shard, accumulator)
        per_source.append(_source_stats(shard, accumulator.areas[start:]))
    return _build_stats(accumulator, per_source)


def render_geometry_stats(stats: GeometryStats) -> str:
    """Render the canonical ``stats.json`` text for ``stats``."""
    return json.dumps(asdict(stats), indent=2, sort_keys=True) + "\n"


def _selected_shards(directory: Path, source_names: Collection[str] | None) -> list[Path]:
    """Return the sorted public shards a card or report covers."""
    paths = sorted(directory.glob("*.parquet"))
    if source_names is None:
        return paths
    stems = {name.removesuffix(".osm.pbf") for name in source_names}
    return [path for path in paths if path.stem in stems]


@dataclass
class _Accumulator:
    """Mutable, bounded accumulation state shared by every selected shard."""

    areas: array[float] = field(default_factory=lambda: array("d"))
    vertices: array[float] = field(default_factory=lambda: array("d"))
    rings: array[float] = field(default_factory=lambda: array("d"))
    components: array[float] = field(default_factory=lambda: array("d"))
    widths_degrees: array[float] = field(default_factory=lambda: array("d"))
    heights_degrees: array[float] = field(default_factory=lambda: array("d"))
    widths_m: array[float] = field(default_factory=lambda: array("d"))
    heights_m: array[float] = field(default_factory=lambda: array("d"))
    polygon_row_count: int = 0
    multipolygon_row_count: int = 0
    with_holes_row_count: int = 0
    hole_ring_count: int = 0
    zero_area_row_count: int = 0
    below_one_m2_row_count: int = 0
    antimeridian_row_count: int = 0
    polar_row_count: int = 0
    buckets: Counter[str] = field(default_factory=Counter)
    tag_row_counts: Counter[str] = field(default_factory=Counter)
    tag_areas: dict[str, array[float]] = field(default_factory=dict)
    bbox: list[float] | None = None


def _accumulate_shard(shard: Path, accumulator: _Accumulator) -> None:
    """Fold one public shard into ``accumulator`` one record batch at a time."""
    parquet = pq.ParquetFile(shard)
    missing = [name for name in GEOMETRY_STATS_COLUMNS if name not in parquet.schema_arrow.names]
    if missing:
        raise ValueError(f"public shard {shard} lacks geometry columns: {sorted(missing)}")
    for batch in parquet.iter_batches(columns=list(GEOMETRY_STATS_COLUMNS), batch_size=BATCH_ROWS):
        _accumulate_batch(accumulator, batch)


def _accumulate_batch(accumulator: _Accumulator, batch: Any) -> None:
    """Fold one record batch, decoding its geometry strings exactly once."""
    boxes = [_parse_bbox(value) for value in batch.column("bbox").to_pylist()]
    for geometry, box, area, tag in zip(
        batch.column("geometry").to_pylist(),
        boxes,
        batch.column("area_m2").to_pylist(),
        batch.column("osm_primary_tag").to_pylist(),
        strict=True,
    ):
        _accumulate_area(accumulator, float(area), str(tag))
        _accumulate_shape(accumulator, str(geometry))
        _accumulate_extent(accumulator, box)
    _accumulate_bbox_metres(accumulator, boxes)


def _accumulate_area(accumulator: _Accumulator, area_m2: float, primary_tag: str) -> None:
    """Record one row's surface, its bucket, and its per-tag contribution."""
    accumulator.areas.append(area_m2)
    accumulator.buckets[_area_bucket(area_m2)] += 1
    if area_m2 == 0.0:
        accumulator.zero_area_row_count += 1
    if area_m2 < TINY_AREA_M2:
        accumulator.below_one_m2_row_count += 1
    accumulator.tag_row_counts[primary_tag] += 1
    accumulator.tag_areas.setdefault(primary_tag, array("d")).append(area_m2)


def _accumulate_shape(accumulator: _Accumulator, geometry: str) -> None:
    """Record one row's component, ring, and vertex structure."""
    kind, components, rings, vertices = _geometry_shape(geometry)
    if kind == "MultiPolygon":
        accumulator.multipolygon_row_count += 1
        accumulator.components.append(float(components))
    else:
        accumulator.polygon_row_count += 1
    holes = rings - components if rings else 0
    if holes > 0:
        accumulator.with_holes_row_count += 1
        accumulator.hole_ring_count += holes
    accumulator.rings.append(float(rings))
    accumulator.vertices.append(float(vertices))


def _accumulate_extent(accumulator: _Accumulator, box: tuple[float, float, float, float]) -> None:
    """Record one row's bounding box in degrees and widen the dataset extent."""
    min_lon, min_lat, max_lon, max_lat = box
    accumulator.widths_degrees.append(max_lon - min_lon)
    accumulator.heights_degrees.append(max_lat - min_lat)
    if max_lon - min_lon > ANTIMERIDIAN_SPAN_DEGREES:
        accumulator.antimeridian_row_count += 1
    if max(abs(min_lat), abs(max_lat)) >= POLAR_LATITUDE_DEGREES:
        accumulator.polar_row_count += 1
    accumulator.bbox = _widen_bbox(accumulator.bbox, box)


def _accumulate_bbox_metres(
    accumulator: _Accumulator,
    boxes: Sequence[tuple[float, float, float, float]],
) -> None:
    """Record geodesic bounding-box dimensions for one batch.

    Width is the geodesic distance across the box at its mid-latitude; height
    is the geodesic distance along its mid-longitude meridian.
    """
    if not boxes:
        return
    mid_lats = _midpoints(boxes, 1, 3)
    mid_lons = _midpoints(boxes, 0, 2)
    accumulator.widths_m.extend(
        _geodesic_lengths(_coordinates(boxes, 0), mid_lats, _coordinates(boxes, 2), mid_lats)
    )
    accumulator.heights_m.extend(
        _geodesic_lengths(mid_lons, _coordinates(boxes, 1), mid_lons, _coordinates(boxes, 3))
    )


def _coordinates(
    boxes: Sequence[tuple[float, float, float, float]],
    index: int,
) -> list[float]:
    """Return one bounding-box coordinate for every box of a batch."""
    return [box[index] for box in boxes]


def _midpoints(
    boxes: Sequence[tuple[float, float, float, float]],
    low: int,
    high: int,
) -> list[float]:
    """Return the midpoint of one bounding-box axis for every box of a batch."""
    return [(box[low] + box[high]) / 2 for box in boxes]


def _geodesic_lengths(
    lons1: list[float],
    lats1: list[float],
    lons2: list[float],
    lats2: list[float],
) -> list[float]:
    """Return WGS84 geodesic lengths in metres for one batch of segments."""
    _, _, distances = _GEOD.inv(lons1, lats1, lons2, lats2)
    return [float(value) for value in distances]


def _widen_bbox(
    current: list[float] | None,
    box: tuple[float, float, float, float],
) -> list[float]:
    """Return the dataset bounding box widened by one row's box."""
    if current is None:
        return list(box)
    return [
        min(current[0], box[0]),
        min(current[1], box[1]),
        max(current[2], box[2]),
        max(current[3], box[3]),
    ]


def _parse_bbox(value: object) -> tuple[float, float, float, float]:
    """Parse the stored ``[min_lon, min_lat, max_lon, max_lat]`` JSON array."""
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or len(decoded) != 4:
        raise ValueError(f"invalid bbox value: {value!r}")
    min_lon, min_lat, max_lon, max_lat = (float(item) for item in decoded)
    return min_lon, min_lat, max_lon, max_lat


def _geometry_shape(geometry: str) -> tuple[str, int, int, int]:
    """Return ``(kind, component count, ring count, vertex count)``."""
    decoded = json.loads(geometry)
    kind = str(decoded["type"])
    parts = _geometry_parts(kind, decoded["coordinates"])
    rings = _component_rings(parts)
    return kind, len(parts), len(rings), _vertex_count(rings)


def _geometry_parts(kind: str, coordinates: Any) -> list[Any]:
    """Return one coordinate list per polygon component of a geometry."""
    if kind == "MultiPolygon":
        return list(coordinates)
    if kind != "Polygon":
        raise ValueError(f"unsupported geometry type: {kind!r}")
    return [coordinates]


def _component_rings(parts: Sequence[Any]) -> list[Any]:
    """Return every ring of every component, outer and inner alike."""
    return [ring for part in parts for ring in part]


def _vertex_count(rings: Sequence[Any]) -> int:
    """Return the total number of stored coordinate pairs."""
    return sum(len(ring) for ring in rings)


def _area_bucket(area_m2: float) -> str:
    """Return the stable log-scale bucket label for one area."""
    if area_m2 == 0.0:
        return "0"
    return AREA_BUCKET_LABELS[bisect_right(_AREA_BUCKET_EDGES, area_m2) + 1]


def _summarize(values: Sequence[float]) -> NumericSummary:
    """Return the exact summary of one distribution, zeroed when empty."""
    if not values:
        return NumericSummary()
    ordered = sorted(values)
    total = math.fsum(ordered)
    percentiles = {f"p{percentile}": _percentile(ordered, percentile) for percentile in PERCENTILES}
    return NumericSummary(
        row_count=len(ordered),
        total=_round(total),
        minimum=_round(ordered[0]),
        maximum=_round(ordered[-1]),
        mean=_round(total / len(ordered)),
        median=percentiles["p50"],
        percentiles=percentiles,
    )


def _percentile(ordered: Sequence[float], percentile: int) -> float:
    """Return the exact nearest-rank percentile of a sorted distribution."""
    rank = math.ceil(percentile / 100 * len(ordered))
    return _round(ordered[max(rank, 1) - 1])


def _round(value: float) -> float:
    """Round one reported value so equal input yields byte-equal output."""
    return round(value, ROUNDING_DECIMALS)


def _source_stats(shard: Path, areas: Sequence[float]) -> SourceAreaStats:
    """Summarize one shard's contribution, named by its source PBF."""
    return SourceAreaStats(
        source_pbf=f"{shard.stem}.osm.pbf",
        row_count=len(areas),
        area_m2=_summarize(areas),
    )


def _build_stats(
    accumulator: _Accumulator,
    per_source: list[SourceAreaStats],
) -> GeometryStats:
    """Convert accumulated state into the reported statistics."""
    return GeometryStats(
        row_count=len(accumulator.areas),
        area=AreaStats(
            summary=_summarize(accumulator.areas),
            histogram=_histogram(accumulator.buckets),
            zero_area_row_count=accumulator.zero_area_row_count,
            below_one_m2_row_count=accumulator.below_one_m2_row_count,
        ),
        shape=ShapeStats(
            polygon_row_count=accumulator.polygon_row_count,
            multipolygon_row_count=accumulator.multipolygon_row_count,
            with_holes_row_count=accumulator.with_holes_row_count,
            hole_ring_count=accumulator.hole_ring_count,
            vertices_per_row=_summarize(accumulator.vertices),
            rings_per_row=_summarize(accumulator.rings),
            components_per_multipolygon=_summarize(accumulator.components),
        ),
        extent=ExtentStats(
            bbox=_rounded_bbox(accumulator.bbox),
            width_degrees=_summarize(accumulator.widths_degrees),
            height_degrees=_summarize(accumulator.heights_degrees),
            width_m=_summarize(accumulator.widths_m),
            height_m=_summarize(accumulator.heights_m),
            antimeridian_row_count=accumulator.antimeridian_row_count,
            polar_row_count=accumulator.polar_row_count,
        ),
        per_source=per_source,
        per_osm_primary_tag=_primary_tag_stats(accumulator),
    )


def _rounded_bbox(bbox: list[float] | None) -> list[float] | None:
    """Round the dataset bounding box, preserving its absence."""
    return None if bbox is None else [_round(value) for value in bbox]


def _histogram(buckets: Counter[str]) -> list[dict[str, Any]]:
    """Render every log-scale bucket in order, including empty ones."""
    return [{"bucket": label, "row_count": buckets[label]} for label in AREA_BUCKET_LABELS]


def _primary_tag_stats(accumulator: _Accumulator) -> list[PrimaryTagAreaStats]:
    """Summarize surface per primary OSM tag, most rows first."""
    stats = [
        PrimaryTagAreaStats(
            osm_primary_tag=tag,
            row_count=accumulator.tag_row_counts[tag],
            total_area_m2=_round(math.fsum(areas)),
        )
        for tag, areas in accumulator.tag_areas.items()
    ]
    return sorted(stats, key=lambda item: (-item.row_count, item.osm_primary_tag))


__all__ = [
    "AREA_BUCKET_LABELS",
    "BATCH_ROWS",
    "GEOMETRY_STATS_COLUMNS",
    "GEOMETRY_STATS_FILENAME",
    "GEOMETRY_STATS_SCHEMA_VERSION",
    "PERCENTILES",
    "AreaStats",
    "ExtentStats",
    "GeometryStats",
    "NumericSummary",
    "PrimaryTagAreaStats",
    "ShapeStats",
    "SourceAreaStats",
    "compute_geometry_stats",
    "render_geometry_stats",
]
