"""Analyze a finished run with DuckDB-backed bounded memory.

The analyzer uses :mod:`osm_polygon_website_tag.storage.duckdb_engine` to
canonicalise observations via SQL window functions, compute the
eight-cell contingency cube at observation and canonical levels, and
aggregate hostnames exactly. All output tables are written via
DuckDB ``COPY TO`` so the bytes are deterministic across runs.

Output tables in ``<run_dir>/analysis/``:

* ``cells_global.parquet`` -- 16 rows: eight cells at observation and
  canonical levels.
* ``cells_by_source.parquet`` -- per-PBF eight-cell counts at
  observation level.
* ``cells_by_region.parquet`` -- per-region eight-cell counts at
  canonical level.
* ``cells_by_osm_type.parquet`` -- per-way/relation canonical cells.
* ``cells_by_primary_category.parquet`` -- per-category eight-cell
  counts at canonical level.
* ``by_website_class_canonical.parquet`` -- per-class canonical counts.
* ``by_contact_website_class_canonical.parquet`` -- per-class canonical counts.
* ``by_source_overlap.parquet`` -- per-PBF public + observation + rejection counts.
* ``by_source_dedup.parquet`` -- per-PBF unique canonical-object count.
* ``duplicate_observations.parquet`` -- (osm_type, osm_id, count) for ids seen >1.
* ``conflicting_snapshots.parquet`` -- non-canonical snapshots that
  disagree with the canonical winner on a tag value.
* ``rejections_by_kind.parquet`` -- exact rejection counts by reason.
* ``hostnames_exact_website.parquet`` -- exact website hostnames.
* ``hostnames_exact_contact_website.parquet`` -- exact contact:website hostnames.
* ``top_hostnames_website.parquet`` -- top 1000 website hostnames.
* ``top_hostnames_contact_website.parquet`` -- top 1000 contact:website hostnames.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from _duckdb._func import FunctionNullHandling

from osm_polygon_website_tag.domain.website import extract_hostname
from osm_polygon_website_tag.storage import duckdb_engine
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.duckdb_engine import EIGHT_CELL_EXPRESSIONS, EIGHT_CELL_LABELS

# Prefix used for the per-invocation, run-owned analysis staging directory.
# Every call to ``analyze_results`` creates a freshly-named subdirectory
# of ``<run_dir>/staging/`` carrying this prefix; only that unique
# directory is removed during cleanup, so a leftover, mis-named, or
# diagnostic sibling directory is never touched.
_ANALYSIS_STAGING_PREFIX = "analysis-"


def _duckdb_extract_hostname(value: str | None) -> str | None:
    """Adapt nullable Parquet values to the pure hostname extractor."""
    return extract_hostname(value) if value is not None else None


ANALYSIS_FILES: tuple[str, ...] = (
    "cells_global.parquet",
    "cells_by_source.parquet",
    "cells_by_region.parquet",
    "cells_by_osm_type.parquet",
    "cells_by_primary_category.parquet",
    "by_website_class_canonical.parquet",
    "by_contact_website_class_canonical.parquet",
    "by_source_overlap.parquet",
    "by_source_dedup.parquet",
    "duplicate_observations.parquet",
    "conflicting_snapshots.parquet",
    "rejections_by_kind.parquet",
    "hostnames_exact_website.parquet",
    "hostnames_exact_contact_website.parquet",
    "top_hostnames_website.parquet",
    "top_hostnames_contact_website.parquet",
)

TOP_K_HOSTNAMES = 1000


@dataclass(frozen=True)
class AnalysisSummary:
    """Top-level counts returned by :func:`analyze_results`."""

    observation_count: int
    canonical_count: int
    public_row_count: int
    rejection_count: int
    duplicate_count: int
    conflicting_snapshot_count: int
    cell_observation: dict[str, int]
    cell_canonical: dict[str, int]


def _public_row_count(polygons_dir: Path) -> int:
    return sum(
        int(pq.ParquetFile(path).metadata.num_rows) for path in polygons_dir.glob("*.parquet")
    )


def _rejection_count(rejections_dir: Path) -> int:
    return sum(
        int(pq.ParquetFile(path).metadata.num_rows) for path in rejections_dir.glob("*.parquet")
    )


def _parquet_row_count(path: Path) -> int:
    return int(pq.ParquetFile(path).metadata.num_rows)


def _cleanup_invocation_staging_dir(path: Path) -> None:
    """Best-effort removal of the per-invocation analysis staging directory.

    ``path`` is the unique staging directory created by the current
    ``analyze_results`` call via :func:`tempfile.mkdtemp`. Every file
    inside it was written by this invocation, so removing the entire
    tree is safe and bounded.

    Cleanup failures are intentionally suppressed: the original analysis
    exception (or successful return) must remain visible to the caller.
    A leftover per-invocation staging tree cannot block a retry because
    subsequent calls create their own uniquely-named directory and never
    touch ``path`` again.
    """
    if not path.exists():
        return
    # Cleanup must never mask the original analysis exception; a leftover
    # per-invocation staging tree cannot block a retry because subsequent
    # calls create their own uniquely-named directory.
    with contextlib.suppress(OSError):
        shutil.rmtree(path)


def analyze_results(run_dir: Path | str) -> AnalysisSummary:
    """Compute every analysis table and write it under
    ``<run_dir>/analysis/``. Returns an :class:`AnalysisSummary`.

    The analyzer writes every Parquet into an invocation-owned, uniquely
    named staging directory under ``<run_dir>/staging/`` and atomically
    promotes the complete bundle into the final ``<run_dir>/analysis/``
    location. The staging directory is removed on success, ordinary
    exceptions, and ``BaseException`` (including ``KeyboardInterrupt``),
    so an interrupted or failed invocation never blocks a later retry.

    Pre-existing subdirectories of ``<run_dir>/staging/`` -- including
    any diagnostic or mis-named directory left by an older interrupted
    run -- are never inspected, reused, or deleted. The DuckDB spill
    directory and the existing all-old-or-all-new promotion contract
    are preserved.
    """
    run_dir = Path(run_dir)
    polygons_dir = run_dir / "polygons"
    obs_dir = run_dir / "analysis_observations"
    rej_dir = run_dir / "rejections"
    final_analysis_dir = run_dir / "analysis"
    final_analysis_dir.mkdir(parents=True, exist_ok=True)
    staging_root = run_dir / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    analysis_dir = Path(tempfile.mkdtemp(prefix=_ANALYSIS_STAGING_PREFIX, dir=staging_root))
    try:
        _validate_analysis_inputs(run_dir, polygons_dir, obs_dir, rej_dir)
        con = _register_analysis_sources(run_dir, obs_dir, polygons_dir, rej_dir)
        try:
            summary = _write_analysis_tables(con, obs_dir, polygons_dir, rej_dir, analysis_dir)
        finally:
            _close_analysis_connection(con)
        atomic_promote_bundle(
            [
                (analysis_dir / filename, final_analysis_dir / filename)
                for filename in ANALYSIS_FILES
            ]
        )
        duckdb_engine.cleanup_temp_dir(run_dir)
        return summary
    finally:
        _cleanup_invocation_staging_dir(analysis_dir)


def _validate_analysis_inputs(
    run_dir: Path, polygons_dir: Path, obs_dir: Path, rej_dir: Path
) -> None:
    """Require the three source artifact directories before opening DuckDB."""
    if not polygons_dir.exists() or not obs_dir.exists() or not rej_dir.exists():
        raise FileNotFoundError(
            f"missing one of polygons/analysis_observations/rejections under {run_dir}"
        )


def _register_analysis_sources(
    run_dir: Path, obs_dir: Path, polygons_dir: Path, rej_dir: Path
) -> duckdb.DuckDBPyConnection:
    """Open DuckDB and register all source views used by analysis queries."""
    con = duckdb_engine.fresh_connection(run_dir)
    try:
        duckdb_engine.register_comparison_parquets(con, obs_dir)
        duckdb_engine.register_public_parquets(con, polygons_dir)
        duckdb_engine.register_rejection_parquets(con, rej_dir)
        duckdb_engine.canonical_observations(con)
    except BaseException:
        _close_analysis_connection(con)
        raise
    return con


def _close_analysis_connection(con: duckdb.DuckDBPyConnection) -> None:
    """Close DuckDB without masking an analysis failure."""
    with contextlib.suppress(Exception):
        con.close()


def _write_analysis_tables(
    con: duckdb.DuckDBPyConnection,
    obs_dir: Path,
    polygons_dir: Path,
    rej_dir: Path,
    analysis_dir: Path,
) -> AnalysisSummary:
    """Write all deterministic analysis tables and return their summary."""
    cells_obs, cells_canon = _write_cell_tables(con, obs_dir, analysis_dir)
    _write_class_tables(con, analysis_dir)
    _write_overlap_tables(con, analysis_dir)
    _write_hostname_tables(con, analysis_dir)
    return _analysis_summary(
        con,
        polygons_dir,
        rej_dir,
        analysis_dir,
        cells_obs=cells_obs,
        cells_canon=cells_canon,
    )


def _write_cell_tables(
    con: duckdb.DuckDBPyConnection, obs_dir: Path, analysis_dir: Path
) -> tuple[dict[str, int], dict[str, int]]:
    """Write global and grouped H3 cell tables."""
    cells_obs = duckdb_engine.cells_global_observation(con)[0]
    cells_canon = duckdb_engine.cells_global_canonical(con)[0]
    rows = _global_cell_rows(cells_obs, cells_canon)
    _write_arrow_table(
        analysis_dir / "cells_global.parquet",
        rows,
        pa.schema(
            [
                pa.field("cell", pa.string(), nullable=False),
                pa.field("level", pa.string(), nullable=False),
                pa.field("row_count", pa.int64(), nullable=False),
            ]
        ),
    )
    _write_cells_per_group(con, obs_dir, analysis_dir / "cells_by_source.parquet")
    for group_column, filename in (
        ("region", "cells_by_region.parquet"),
        ("osm_type", "cells_by_osm_type.parquet"),
        ("primary_category", "cells_by_primary_category.parquet"),
    ):
        _write_cells_per_group(
            con,
            obs_dir,
            analysis_dir / filename,
            group_column=group_column,
            view="canonical_observations",
        )
    return cells_obs, cells_canon


def _global_cell_rows(
    cells_obs: dict[str, int], cells_canon: dict[str, int]
) -> list[dict[str, object]]:
    """Build the deterministic 16-row observation/canonical cell table."""
    rows: list[dict[str, object]] = []
    for key, _ in EIGHT_CELL_LABELS:
        rows.append({"cell": key, "level": "observation", "row_count": int(cells_obs.get(key, 0))})
        rows.append({"cell": key, "level": "canonical", "row_count": int(cells_canon.get(key, 0))})
    return rows


def _write_class_tables(con: duckdb.DuckDBPyConnection, analysis_dir: Path) -> None:
    """Write canonical website classification tables."""
    for column, filename in (
        ("website_class", "by_website_class_canonical.parquet"),
        ("contact_website_class", "by_contact_website_class_canonical.parquet"),
    ):
        _write_class_count(con, analysis_dir / filename, column=column, view="public_polygons")


def _write_overlap_tables(con: duckdb.DuckDBPyConnection, analysis_dir: Path) -> None:
    """Write source overlap, deduplication, conflict, and rejection tables."""
    queries = (
        (
            """
            WITH public_counts AS (
              SELECT source_pbf, COUNT(*)::BIGINT AS public_row_count
              FROM public_polygons GROUP BY source_pbf
            ), observation_counts AS (
              SELECT source_pbf, COUNT(*)::BIGINT AS observation_row_count
              FROM observations GROUP BY source_pbf
            ), rejection_counts AS (
              SELECT source_pbf, COUNT(*)::BIGINT AS rejection_count
              FROM rejection_rows GROUP BY source_pbf
            )
            SELECT COALESCE(p.source_pbf, o.source_pbf, r.source_pbf) AS source_pbf,
                   COALESCE(public_row_count, 0)::BIGINT AS public_row_count,
                   COALESCE(observation_row_count, 0)::BIGINT AS observation_row_count,
                   COALESCE(rejection_count, 0)::BIGINT AS rejection_count
            FROM public_counts p FULL OUTER JOIN observation_counts o USING (source_pbf)
            FULL OUTER JOIN rejection_counts r
              ON COALESCE(p.source_pbf, o.source_pbf) = r.source_pbf
            ORDER BY source_pbf
            """,
            "by_source_overlap.parquet",
        ),
        (
            """
            SELECT source_pbf, COUNT(*)::BIGINT AS unique_canonical_count
            FROM canonical_observations GROUP BY source_pbf ORDER BY source_pbf
            """,
            "by_source_dedup.parquet",
        ),
        (
            """
            SELECT osm_type, osm_id, COUNT(*)::BIGINT AS observation_count
            FROM observations GROUP BY osm_type, osm_id HAVING COUNT(*) > 1
            ORDER BY osm_type, osm_id
            """,
            "duplicate_observations.parquet",
        ),
        (
            """
            WITH ranked AS (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY osm_type, osm_id
                ORDER BY osm_version DESC, osm_timestamp DESC, source_pbf ASC
              ) AS rn FROM observations
            )
            SELECT n.osm_type, n.osm_id,
              c.source_pbf AS canonical_source_pbf, n.source_pbf AS observed_source_pbf,
              c.website AS canonical_website, n.website AS observed_website,
              c.contact_website AS canonical_contact_website,
              n.contact_website AS observed_contact_website,
              c.wikidata AS canonical_wikidata, n.wikidata AS observed_wikidata
            FROM ranked n JOIN ranked c
              ON n.osm_type=c.osm_type AND n.osm_id=c.osm_id AND c.rn=1
            WHERE n.rn > 1 AND (
              (c.website IS NOT DISTINCT FROM n.website) IS FALSE OR
              (c.contact_website IS NOT DISTINCT FROM n.contact_website) IS FALSE OR
              (c.wikidata IS NOT DISTINCT FROM n.wikidata) IS FALSE
            ) ORDER BY n.osm_type, n.osm_id, n.source_pbf
            """,
            "conflicting_snapshots.parquet",
        ),
        (
            """
            SELECT rejection_kind, COUNT(*)::BIGINT AS row_count
            FROM rejection_rows GROUP BY rejection_kind ORDER BY rejection_kind
            """,
            "rejections_by_kind.parquet",
        ),
    )
    for query, filename in queries:
        duckdb_engine.copy_query_atomic(con, query, analysis_dir / filename)


def _write_hostname_tables(con: duckdb.DuckDBPyConnection, analysis_dir: Path) -> None:
    """Write exact and top canonical hostname tables."""
    con.create_function(
        "normalize_hostname",
        _duckdb_extract_hostname,
        ["VARCHAR"],
        "VARCHAR",
        null_handling=FunctionNullHandling.SPECIAL,
    )
    for raw_column, output_column in (
        ("website", "website_hostname"),
        ("contact_website", "contact_website_hostname"),
    ):
        exact_query = f"""
            SELECT normalize_hostname({raw_column}) AS {output_column},
                   COUNT(*)::BIGINT AS row_count
            FROM canonical_observations
            WHERE normalize_hostname({raw_column}) IS NOT NULL
            GROUP BY 1 ORDER BY row_count DESC, {output_column}
        """  # noqa: S608
        duckdb_engine.copy_query_atomic(
            con, exact_query, analysis_dir / f"hostnames_exact_{raw_column}.parquet"
        )
        duckdb_engine.copy_query_atomic(
            con,
            f"SELECT * FROM ({exact_query}) LIMIT {TOP_K_HOSTNAMES}",  # noqa: S608
            analysis_dir / f"top_hostnames_{raw_column}.parquet",
        )


def _analysis_summary(
    con: duckdb.DuckDBPyConnection,
    polygons_dir: Path,
    rej_dir: Path,
    analysis_dir: Path,
    *,
    cells_obs: dict[str, int],
    cells_canon: dict[str, int],
) -> AnalysisSummary:
    """Build the top-level analysis summary from written tables."""
    observation_count = con.execute("SELECT COUNT(*) FROM observations").fetchone()
    canonical_count = con.execute("SELECT COUNT(*) FROM canonical_observations").fetchone()
    return AnalysisSummary(
        observation_count=int(observation_count[0]) if observation_count else 0,
        canonical_count=int(canonical_count[0]) if canonical_count else 0,
        public_row_count=_public_row_count(polygons_dir),
        rejection_count=_rejection_count(rej_dir),
        duplicate_count=_parquet_row_count(analysis_dir / "duplicate_observations.parquet"),
        conflicting_snapshot_count=_parquet_row_count(
            analysis_dir / "conflicting_snapshots.parquet"
        ),
        cell_observation={k: int(cells_obs.get(k, 0)) for k, _ in EIGHT_CELL_LABELS},
        cell_canonical={k: int(cells_canon.get(k, 0)) for k, _ in EIGHT_CELL_LABELS},
    )


def _write_arrow_table(path: Path, rows: list[dict[str, object]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    pq.write_table(table, path, compression="snappy")


def _write_cells_per_group(
    con: duckdb.DuckDBPyConnection,
    obs_dir: Path,
    out_path: Path,
    *,
    group_column: str = "source_pbf",
    view: str = "observations",
) -> None:
    """Write per-group cells directly with DuckDB external memory."""
    allowed = {"source_pbf", "region", "primary_category", "osm_type"}
    if group_column not in allowed or view not in {
        "observations",
        "canonical_observations",
    }:
        raise ValueError("unsupported group query")
    parts = [
        (
            f"SELECT {group_column}::VARCHAR AS group_value, '{key}' AS cell, "  # noqa: S608
            f"'{label}' AS cell_label, SUM({EIGHT_CELL_EXPRESSIONS[key]})::BIGINT "
            f"AS row_count FROM {view} GROUP BY {group_column}"
        )
        for key, label in EIGHT_CELL_LABELS
    ]
    duckdb_engine.copy_query_atomic(
        con,
        " UNION ALL ".join(parts) + " ORDER BY group_value, cell",
        out_path,
    )


def _write_class_count(
    con: duckdb.DuckDBPyConnection,
    out_path: Path,
    *,
    column: str,
    view: str = "canonical_observations",
) -> None:
    """Per-class canonical count for ``column`` (nullable string column)."""
    if column not in {"website_class", "contact_website_class"} or view != "public_polygons":
        raise ValueError("unsupported class query")
    duckdb_engine.copy_query_atomic(
        con,
        f"""
        SELECT {column} AS class_value, COUNT(*) AS row_count
        FROM {view}
        WHERE {column} IS NOT NULL
        GROUP BY {column}
        ORDER BY {column}
        """,  # noqa: S608
        out_path,
    )


__all__ = ["ANALYSIS_FILES", "AnalysisSummary", "analyze_results"]
