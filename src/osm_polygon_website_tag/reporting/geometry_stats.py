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
* Totals use DuckDB's deterministic ``fsum`` aggregate.
* Percentiles are exact nearest-rank values over every row.
* Every reported float is rounded to :data:`ROUNDING_DECIMALS` decimals, so
  unchanged input produces byte-identical output.

Memory
------

Geometry strings are decoded one record batch at a time and never retained.
The derived row metrics are inserted into the run-owned, spill-enabled
DuckDB store; exact percentile queries sort there rather than retaining
Python arrays for the whole dataset.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pyproj

from osm_polygon_website_tag.reporting.text_population import (
    TextPopulationSummary,
    compute_text_population_summary,
)
from osm_polygon_website_tag.storage.duckdb_engine import fresh_connection

GEOMETRY_STATS_FILENAME = "stats.json"
GEOMETRY_STATS_SCHEMA_VERSION = "v2"

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
    population_scope: str = "published polygon rows"
    text_population_scope: str = (
        "global unique (osm_type, osm_id) identities with successful non-empty website or "
        "contact:website text"
    )
    text_population: TextPopulationSummary = field(default_factory=TextPopulationSummary)
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
    text_population: TextPopulationSummary | None = None,
) -> GeometryStats:
    """Compute geometry statistics from every selected public polygon row.

    Raises :class:`FileNotFoundError` when ``polygons/`` is missing and
    :class:`ValueError` when a selected shard lacks a required column.
    """
    root = Path(run_dir)
    directory = root / "polygons"
    if not directory.is_dir():
        raise FileNotFoundError(f"missing {directory}")
    resolved_text_population = (
        text_population
        if text_population is not None
        else compute_text_population_summary(root, source_names=source_names)
    )
    accumulator = _Accumulator()
    store = _GeometryValueStore(root)
    try:
        selected_shards = _selected_shards(directory, source_names)
        source_pbf_names = [f"{shard.stem}.osm.pbf" for shard in selected_shards]
        for shard in selected_shards:
            _accumulate_shard(shard, accumulator, store)
        return _build_stats(
            accumulator,
            store,
            source_pbf_names,
            text_population=resolved_text_population,
        )
    finally:
        store.close()


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


_SPILL_SCHEMA = pa.schema(
    [
        ("source_pbf", pa.string()),
        ("osm_primary_tag", pa.string()),
        ("area_m2", pa.float64()),
        ("vertices", pa.float64()),
        ("rings", pa.float64()),
        ("components", pa.float64()),
        ("width_degrees", pa.float64()),
        ("height_degrees", pa.float64()),
        ("width_m", pa.float64()),
        ("height_m", pa.float64()),
    ]
)

_SPILL_NUMERIC_COLUMNS = frozenset(
    {
        "area_m2",
        "vertices",
        "rings",
        "components",
        "width_degrees",
        "height_degrees",
        "width_m",
        "height_m",
    }
)


class _GeometryValueStore:
    """Run-owned DuckDB store for exact, spillable numeric distributions."""

    def __init__(self, run_dir: Path) -> None:
        # Serial: these aggregates sum doubles, and parallel summation reorders
        # the additions, which changes the last digits and breaks byte stability.
        self._connection = fresh_connection(run_dir)
        self._connection.execute(
            """
            CREATE TABLE geometry_stats_values (
                source_pbf VARCHAR NOT NULL,
                osm_primary_tag VARCHAR NOT NULL,
                area_m2 DOUBLE,
                vertices DOUBLE,
                rings DOUBLE,
                components DOUBLE,
                width_degrees DOUBLE,
                height_degrees DOUBLE,
                width_m DOUBLE,
                height_m DOUBLE
            )
            """
        )

    def append(self, rows: list[dict[str, Any]]) -> None:
        """Append one bounded record batch to the external-memory table."""
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=_SPILL_SCHEMA)
        view_name = "geometry_stats_batch"
        self._connection.register(view_name, table)
        try:
            self._connection.execute(
                "INSERT INTO geometry_stats_values SELECT * FROM geometry_stats_batch"
            )
        finally:
            self._connection.unregister(view_name)

    def summary(self, column: str) -> NumericSummary:
        """Return an exact nearest-rank summary, sorting outside Python memory."""
        if column not in _SPILL_NUMERIC_COLUMNS:
            raise ValueError(f"unsupported geometry statistics column: {column}")
        percentile_exprs = ", ".join(
            f"MAX(value) FILTER (WHERE ordinal = CAST(CEIL(row_count * {percentile / 100}) AS BIGINT)) "
            f"AS p{percentile}"
            for percentile in PERCENTILES
        )
        row = self._connection.execute(
            f"""
            WITH ordered AS (
                SELECT
                    CAST({column} AS DOUBLE) AS value,
                    ROW_NUMBER() OVER (ORDER BY {column}) AS ordinal,
                    COUNT(*) OVER () AS row_count
                FROM geometry_stats_values
                WHERE {column} IS NOT NULL
            )
            SELECT
                COUNT(*) AS row_count,
                COALESCE(FSUM(value), 0.0) AS total,
                COALESCE(MIN(value), 0.0) AS minimum,
                COALESCE(MAX(value), 0.0) AS maximum,
                {percentile_exprs}
            FROM ordered
            """  # noqa: S608
        ).fetchone()
        if row is None:
            return NumericSummary()
        return _summary_from_row(row)

    def source_stats(self, source_pbf_names: Sequence[str]) -> list[SourceAreaStats]:
        """Return exact per-source summaries in canonical source order."""
        percentile_exprs = ", ".join(
            f"MAX(value) FILTER (WHERE ordinal = CAST(CEIL(row_count * {percentile / 100}) AS BIGINT)) "
            f"AS p{percentile}"
            for percentile in PERCENTILES
        )
        rows = self._connection.execute(
            f"""
            WITH ordered AS (
                SELECT
                    source_pbf,
                    CAST(area_m2 AS DOUBLE) AS value,
                    ROW_NUMBER() OVER (
                        PARTITION BY source_pbf ORDER BY area_m2
                    ) AS ordinal,
                    COUNT(*) OVER (PARTITION BY source_pbf) AS row_count
                FROM geometry_stats_values
                WHERE area_m2 IS NOT NULL
            )
            SELECT
                source_pbf,
                COUNT(*) AS row_count,
                COALESCE(FSUM(value), 0.0) AS total,
                COALESCE(MIN(value), 0.0) AS minimum,
                COALESCE(MAX(value), 0.0) AS maximum,
                {percentile_exprs}
            FROM ordered
            GROUP BY source_pbf
            ORDER BY source_pbf
            """  # noqa: S608
        ).fetchall()
        summaries = {
            str(row[0]): SourceAreaStats(
                source_pbf=str(row[0]),
                row_count=int(row[1]),
                area_m2=_summary_from_row(row[1:]),
            )
            for row in rows
        }
        return [
            summaries.get(
                source_pbf,
                SourceAreaStats(
                    source_pbf=source_pbf,
                    row_count=0,
                    area_m2=NumericSummary(),
                ),
            )
            for source_pbf in source_pbf_names
        ]

    def primary_tag_stats(self) -> list[PrimaryTagAreaStats]:
        """Return exact per-tag counts and surface totals in stable order."""
        rows = self._connection.execute(
            """
            SELECT
                osm_primary_tag,
                COUNT(*) AS row_count,
                COALESCE(FSUM(area_m2), 0.0) AS total_area_m2
            FROM geometry_stats_values
            GROUP BY osm_primary_tag
            ORDER BY row_count DESC, osm_primary_tag
            """
        ).fetchall()
        return [
            PrimaryTagAreaStats(
                osm_primary_tag=str(row[0]),
                row_count=int(row[1]),
                total_area_m2=_round(float(row[2])),
            )
            for row in rows
        ]

    def close(self) -> None:
        """Close the connection and release any run-owned spill files."""
        self._connection.close()


@dataclass
class _Accumulator:
    """Mutable scalar counters shared by every selected shard."""

    row_count: int = 0
    polygon_row_count: int = 0
    multipolygon_row_count: int = 0
    with_holes_row_count: int = 0
    hole_ring_count: int = 0
    zero_area_row_count: int = 0
    below_one_m2_row_count: int = 0
    antimeridian_row_count: int = 0
    polar_row_count: int = 0
    buckets: Counter[str] = field(default_factory=Counter)
    bbox: list[float] | None = None


def _accumulate_shard(
    shard: Path,
    accumulator: _Accumulator,
    store: _GeometryValueStore,
) -> None:
    """Fold one public shard into scalar state and the spillable store."""
    parquet = pq.ParquetFile(shard)
    missing = [name for name in GEOMETRY_STATS_COLUMNS if name not in parquet.schema_arrow.names]
    if missing:
        raise ValueError(f"public shard {shard} lacks geometry columns: {sorted(missing)}")
    for batch in parquet.iter_batches(columns=list(GEOMETRY_STATS_COLUMNS), batch_size=BATCH_ROWS):
        _accumulate_batch(
            accumulator,
            batch,
            source_pbf=f"{shard.stem}.osm.pbf",
            store=store,
        )


def _accumulate_batch(
    accumulator: _Accumulator,
    batch: Any,
    *,
    source_pbf: str,
    store: _GeometryValueStore,
) -> None:
    """Fold one record batch, decoding its geometry strings exactly once."""
    boxes = [_parse_bbox(value) for value in batch.column("bbox").to_pylist()]
    widths_m, heights_m = _bbox_metre_values(boxes)
    rows: list[dict[str, Any]] = []
    for geometry, box, area, tag in zip(
        batch.column("geometry").to_pylist(),
        boxes,
        batch.column("area_m2").to_pylist(),
        batch.column("osm_primary_tag").to_pylist(),
        strict=True,
    ):
        area_value = float(area)
        primary_tag = str(tag)
        kind, components, rings, vertices = _accumulate_shape(accumulator, str(geometry))
        _accumulate_area(accumulator, area_value, primary_tag)
        width_degrees, height_degrees = _accumulate_extent(accumulator, box)
        row_index = len(rows)
        rows.append(
            {
                "source_pbf": source_pbf,
                "osm_primary_tag": primary_tag,
                "area_m2": area_value,
                "vertices": float(vertices),
                "rings": float(rings),
                "components": float(components) if kind == "MultiPolygon" else None,
                "width_degrees": width_degrees,
                "height_degrees": height_degrees,
                "width_m": widths_m[row_index],
                "height_m": heights_m[row_index],
            }
        )
    store.append(rows)


def _accumulate_area(accumulator: _Accumulator, area_m2: float, _primary_tag: str) -> None:
    """Record one row's surface and its bounded area counters."""
    accumulator.row_count += 1
    accumulator.buckets[_area_bucket(area_m2)] += 1
    if area_m2 == 0.0:
        accumulator.zero_area_row_count += 1
    if area_m2 < TINY_AREA_M2:
        accumulator.below_one_m2_row_count += 1


def _accumulate_shape(accumulator: _Accumulator, geometry: str) -> tuple[str, int, int, int]:
    """Record one row's component, ring, and vertex structure."""
    kind, components, rings, vertices = _geometry_shape(geometry)
    if kind == "MultiPolygon":
        accumulator.multipolygon_row_count += 1
    else:
        accumulator.polygon_row_count += 1
    holes = rings - components if rings else 0
    if holes > 0:
        accumulator.with_holes_row_count += 1
        accumulator.hole_ring_count += holes
    return kind, components, rings, vertices


def _accumulate_extent(
    accumulator: _Accumulator,
    box: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Record one row's bounding box in degrees and widen the dataset extent."""
    min_lon, min_lat, max_lon, max_lat = box
    width_degrees = max_lon - min_lon
    height_degrees = max_lat - min_lat
    if width_degrees > ANTIMERIDIAN_SPAN_DEGREES:
        accumulator.antimeridian_row_count += 1
    if max(abs(min_lat), abs(max_lat)) >= POLAR_LATITUDE_DEGREES:
        accumulator.polar_row_count += 1
    accumulator.bbox = _widen_bbox(accumulator.bbox, box)
    return width_degrees, height_degrees


def _bbox_metre_values(
    boxes: Sequence[tuple[float, float, float, float]],
) -> tuple[list[float], list[float]]:
    """Record geodesic bounding-box dimensions for one batch.

    Width is the geodesic distance across the box at its mid-latitude; height
    is the geodesic distance along its mid-longitude meridian.
    """
    if not boxes:
        return [], []
    mid_lats = _midpoints(boxes, 1, 3)
    mid_lons = _midpoints(boxes, 0, 2)
    widths = _geodesic_lengths(_coordinates(boxes, 0), mid_lats, _coordinates(boxes, 2), mid_lats)
    heights = _geodesic_lengths(mid_lons, _coordinates(boxes, 1), mid_lons, _coordinates(boxes, 3))
    return widths, heights


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


def _summary_from_row(row: Sequence[object]) -> NumericSummary:
    """Convert one DuckDB summary row into the canonical numeric model."""
    row_count = _as_int(row[0])
    total = _as_float(row[1])
    minimum = _as_float(row[2])
    maximum = _as_float(row[3])
    percentiles = _summary_percentiles(row, row_count)
    return NumericSummary(
        row_count=row_count,
        total=_round(total),
        minimum=_round(minimum),
        maximum=_round(maximum),
        mean=_round(total / row_count) if row_count else 0.0,
        median=percentiles["p50"] if row_count else 0.0,
        percentiles=percentiles if row_count else {},
    )


def _as_int(value: object) -> int:
    """Convert a nullable DuckDB integer result to a plain integer."""
    if value is None:
        return 0
    return int(cast(Any, value))


def _as_float(value: object) -> float:
    """Convert a nullable DuckDB numeric result to a plain float."""
    if value is None:
        return 0.0
    return float(cast(Any, value))


def _summary_percentiles(row: Sequence[object], row_count: int) -> dict[str, float]:
    """Convert the percentile columns of a non-empty summary row."""
    if not row_count:
        return {}
    return {
        f"p{percentile}": _round(_as_float(row[index]))
        for index, percentile in enumerate(PERCENTILES, start=4)
    }


def _round(value: float) -> float:
    """Round one reported value so equal input yields byte-equal output."""
    return round(value, ROUNDING_DECIMALS)


def _build_stats(
    accumulator: _Accumulator,
    store: _GeometryValueStore,
    source_pbf_names: Sequence[str],
    *,
    text_population: TextPopulationSummary,
) -> GeometryStats:
    """Convert accumulated state into the reported statistics."""
    return GeometryStats(
        text_population=text_population,
        row_count=accumulator.row_count,
        area=AreaStats(
            summary=store.summary("area_m2"),
            histogram=_histogram(accumulator.buckets),
            zero_area_row_count=accumulator.zero_area_row_count,
            below_one_m2_row_count=accumulator.below_one_m2_row_count,
        ),
        shape=ShapeStats(
            polygon_row_count=accumulator.polygon_row_count,
            multipolygon_row_count=accumulator.multipolygon_row_count,
            with_holes_row_count=accumulator.with_holes_row_count,
            hole_ring_count=accumulator.hole_ring_count,
            vertices_per_row=store.summary("vertices"),
            rings_per_row=store.summary("rings"),
            components_per_multipolygon=store.summary("components"),
        ),
        extent=ExtentStats(
            bbox=_rounded_bbox(accumulator.bbox),
            width_degrees=store.summary("width_degrees"),
            height_degrees=store.summary("height_degrees"),
            width_m=store.summary("width_m"),
            height_m=store.summary("height_m"),
            antimeridian_row_count=accumulator.antimeridian_row_count,
            polar_row_count=accumulator.polar_row_count,
        ),
        per_source=store.source_stats(source_pbf_names),
        per_osm_primary_tag=store.primary_tag_stats(),
    )


def _rounded_bbox(bbox: list[float] | None) -> list[float] | None:
    """Round the dataset bounding box, preserving its absence."""
    return None if bbox is None else [_round(value) for value in bbox]


def _histogram(buckets: Counter[str]) -> list[dict[str, Any]]:
    """Render every log-scale bucket in order, including empty ones."""
    return [{"bucket": label, "row_count": buckets[label]} for label in AREA_BUCKET_LABELS]


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
