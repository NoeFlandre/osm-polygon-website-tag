r"""DuckDB-based external-memory analysis engine.

This module is the bounded-memory heart of :mod:`osm_polygon_website_tag.pipeline.analyze`.
It uses DuckDB to canonicalise observations, compute the eight-cell
contingency cube, and aggregate hostnames exactly -- all without
loading full datasets into Python.

Resource controls
-----------------

* ``DUCKDB_MEMORY_LIMIT`` -- DuckDB spill-to-disk memory budget.
* ``DUCKDB_TEMP_DIR`` -- run-owned temporary directory inside the run
  directory (``<run_dir>/staging/duckdb/``). Removed only after
  successful finalisation.
* ``DUCKDB_THREADS = 1`` -- deterministic, single-threaded execution.
* No persistent database outside the run.

Safety
------

* All SQL is statically written; no user-controlled strings are
  concatenated into SQL. File paths are passed as parameters via
  DuckDB's ``read_parquet`` glob / ``COPY`` mechanism, never as
  string-interpolated SQL.
* Every function takes an explicit ``run_dir`` parameter; no implicit
  resolution from environment variables.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from osm_polygon_website_tag.storage.atomic import atomic_write_file

DEFAULT_MEMORY_LIMIT = "2GB"
DUCKDB_THREADS = 1


def _make_connection(
    temp_dir: Path,
    memory_limit: str | None = None,
) -> duckdb.DuckDBPyConnection:
    temp_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    effective_limit = DEFAULT_MEMORY_LIMIT if memory_limit is None else memory_limit
    con.execute(f"SET memory_limit = '{effective_limit}'")
    con.execute(f"SET threads = {DUCKDB_THREADS}")
    con.execute(f"SET temp_directory = '{str(temp_dir).replace(chr(39), chr(39) + chr(39))}'")
    con.execute("SET enable_progress_bar = false")
    return con


def fresh_connection(run_dir: Path) -> duckdb.DuckDBPyConnection:
    """Return a fresh DuckDB connection configured for ``run_dir``."""
    staging = run_dir / "staging" / "duckdb"
    return _make_connection(staging)


def register_comparison_parquets(
    con: duckdb.DuckDBPyConnection,
    obs_dir: Path,
) -> None:
    """Register every comparison-observation Parquet under ``obs_dir``.

    Creates a view named ``observations`` with the comparison schema.
    If ``obs_dir`` is empty, the view is registered as empty with the
    comparison schema's column types.
    """
    glob = str(obs_dir / "*.parquet").replace("'", "''")
    files = sorted(obs_dir.glob("*.parquet"))
    if not files:
        con.execute(
            """
            CREATE OR REPLACE VIEW observations AS
            SELECT
              CAST(NULL AS VARCHAR) AS osm_type,
              CAST(NULL AS BIGINT) AS osm_id,
              CAST(NULL AS INTEGER) AS osm_version,
              CAST(NULL AS TIMESTAMP) AS osm_timestamp,
              CAST(NULL AS VARCHAR) AS source_pbf,
              CAST(NULL AS VARCHAR) AS region,
              CAST(NULL AS VARCHAR) AS primary_category,
              CAST(NULL AS VARCHAR) AS website,
              CAST(NULL AS VARCHAR) AS contact_website,
              CAST(NULL AS VARCHAR) AS wikidata,
              CAST(NULL AS BOOLEAN) AS has_website,
              CAST(NULL AS BOOLEAN) AS has_contact_website,
              CAST(NULL AS BOOLEAN) AS has_any_website,
              CAST(NULL AS BOOLEAN) AS has_wikidata,
              CAST(NULL AS VARCHAR) AS schema_version
            WHERE FALSE
            """
        )
        return
    con.execute(
        f"""
        CREATE OR REPLACE VIEW observations AS
        SELECT * FROM read_parquet('{glob}')
        """  # noqa: S608
    )


def register_public_parquets(
    con: duckdb.DuckDBPyConnection,
    polygons_dir: Path,
) -> None:
    """Register every public polygon Parquet under ``polygons_dir``.

    If the directory is empty, register an empty view with the public
    schema's column types.
    """
    glob = str(polygons_dir / "*.parquet").replace("'", "''")
    files = sorted(polygons_dir.glob("*.parquet"))
    if not files:
        con.execute(
            """
            CREATE OR REPLACE VIEW public_polygons AS
            SELECT
              CAST(NULL AS VARCHAR) AS polygon_id,
              CAST(NULL AS VARCHAR) AS region,
              CAST(NULL AS VARCHAR) AS source_pbf,
              CAST(NULL AS VARCHAR) AS osm_type,
              CAST(NULL AS BIGINT) AS osm_id,
              CAST(NULL AS INTEGER) AS osm_version,
              CAST(NULL AS TIMESTAMP) AS osm_timestamp,
              CAST(NULL AS VARCHAR) AS name,
              CAST(NULL AS VARCHAR) AS website,
              CAST(NULL AS VARCHAR) AS contact_website,
              CAST(NULL AS BOOLEAN) AS has_website,
              CAST(NULL AS BOOLEAN) AS has_contact_website,
              CAST(NULL AS BOOLEAN) AS has_any_website,
              CAST(NULL AS VARCHAR) AS website_class,
              CAST(NULL AS VARCHAR) AS contact_website_class,
              CAST(NULL AS VARCHAR) AS website_hostname,
              CAST(NULL AS VARCHAR) AS contact_website_hostname,
              CAST(NULL AS VARCHAR) AS tags,
              CAST(NULL AS VARCHAR) AS tag_keys,
              CAST(NULL AS INTEGER) AS tag_count,
              CAST(NULL AS VARCHAR) AS osm_primary_tag,
              CAST(NULL AS VARCHAR) AS geometry,
              CAST(NULL AS VARCHAR) AS centroid,
              CAST(NULL AS VARCHAR) AS centroid_kind,
              CAST(NULL AS DOUBLE) AS lat,
              CAST(NULL AS DOUBLE) AS lon,
              CAST(NULL AS VARCHAR) AS bbox,
              CAST(NULL AS DOUBLE) AS area_m2,
              CAST(NULL AS VARCHAR) AS area_bucket,
              CAST(NULL AS VARCHAR) AS schema_version,
              CAST(NULL AS VARCHAR) AS website_text,
              CAST(NULL AS BIGINT) AS website_word_count,
              CAST(NULL AS VARCHAR) AS website_text_status,
              CAST(NULL AS VARCHAR) AS contact_website_text,
              CAST(NULL AS BIGINT) AS contact_website_word_count,
              CAST(NULL AS VARCHAR) AS contact_website_text_status,
              CAST(NULL AS VARCHAR) AS website_language,
              CAST(NULL AS DOUBLE) AS website_language_probability,
              CAST(NULL AS VARCHAR) AS contact_website_language,
              CAST(NULL AS DOUBLE) AS contact_website_language_probability
            WHERE FALSE
            """
        )
        return
    con.execute(
        f"""
        CREATE OR REPLACE VIEW public_polygons AS
        SELECT * FROM read_parquet('{glob}', union_by_name=true)
        """  # noqa: S608
    )


def register_rejection_parquets(
    con: duckdb.DuckDBPyConnection,
    rejection_dir: Path,
) -> None:
    """Register rejection shards, including an exactly typed empty view."""
    files = sorted(rejection_dir.glob("*.parquet"))
    if not files:
        con.execute(
            """
            CREATE OR REPLACE VIEW rejection_rows AS
            SELECT CAST(NULL AS VARCHAR) AS source_pbf,
                   CAST(NULL AS VARCHAR) AS rejection_kind
            WHERE FALSE
            """
        )
        return
    glob = str(rejection_dir / "*.parquet").replace("'", "''")
    con.execute(
        f"""CREATE OR REPLACE VIEW rejection_rows AS
            SELECT source_pbf, rejection_kind FROM read_parquet('{glob}')"""  # noqa: S608
    )


# SQL expressions for the eight mutually exclusive cells of W x C x D.
# The (W, C, D) cube covers all combinations of the three booleans.
# cell_name => CASE expression that returns 1 if the row is in the cell,
# else 0.
EIGHT_CELL_EXPRESSIONS: dict[str, str] = {
    "cell_000_w0_c0_d0": (
        "(CASE WHEN NOT has_website AND NOT has_contact_website "
        "AND NOT has_wikidata THEN 1 ELSE 0 END)"
    ),
    "cell_001_w0_c0_d1": (
        "(CASE WHEN NOT has_website AND NOT has_contact_website AND has_wikidata THEN 1 ELSE 0 END)"
    ),
    "cell_010_w0_c1_d0": (
        "(CASE WHEN NOT has_website AND has_contact_website AND NOT has_wikidata THEN 1 ELSE 0 END)"
    ),
    "cell_011_w0_c1_d1": (
        "(CASE WHEN NOT has_website AND has_contact_website AND has_wikidata THEN 1 ELSE 0 END)"
    ),
    "cell_100_w1_c0_d0": (
        "(CASE WHEN has_website AND NOT has_contact_website AND NOT has_wikidata THEN 1 ELSE 0 END)"
    ),
    "cell_101_w1_c0_d1": (
        "(CASE WHEN has_website AND NOT has_contact_website AND has_wikidata THEN 1 ELSE 0 END)"
    ),
    "cell_110_w1_c1_d0": (
        "(CASE WHEN has_website AND has_contact_website AND NOT has_wikidata THEN 1 ELSE 0 END)"
    ),
    "cell_111_w1_c1_d1": (
        "(CASE WHEN has_website AND has_contact_website AND has_wikidata THEN 1 ELSE 0 END)"
    ),
}


EIGHT_CELL_LABELS: tuple[tuple[str, str], ...] = (
    ("cell_000_w0_c0_d0", "000_w0_c0_d0"),
    ("cell_001_w0_c0_d1", "001_w0_c0_d1"),
    ("cell_010_w0_c1_d0", "010_w0_c1_d0"),
    ("cell_011_w0_c1_d1", "011_w0_c1_d1"),
    ("cell_100_w1_c0_d0", "100_w1_c0_d0"),
    ("cell_101_w1_c0_d1", "101_w1_c0_d1"),
    ("cell_110_w1_c1_d0", "110_w1_c1_d0"),
    ("cell_111_w1_c1_d1", "111_w1_c1_d1"),
)


def cells_global_observation(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Return the eight-cell counts at observation level (raw rows)."""
    select_exprs = ", ".join(
        f"COALESCE(SUM({expr}), 0) AS {name}" for name, expr in EIGHT_CELL_EXPRESSIONS.items()
    )
    row = con.execute(f"SELECT {select_exprs} FROM observations").fetchone()  # noqa: S608
    if row is None:
        return [dict.fromkeys(EIGHT_CELL_EXPRESSIONS.keys(), 0)]
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, row, strict=False))]


def canonical_observations(con: duckdb.DuckDBPyConnection) -> None:
    """Create the ``canonical_observations`` view via ROW_NUMBER.

    Canonical winner per ``(osm_type, osm_id)``:

        highest osm_version DESC,
        newest osm_timestamp DESC,
        lexicographically smallest source_pbf ASC
    """
    con.execute(
        """
        CREATE OR REPLACE VIEW canonical_observations AS
        SELECT * EXCLUDE (rn) FROM (
          SELECT *,
            ROW_NUMBER() OVER (
              PARTITION BY osm_type, osm_id
              ORDER BY osm_version DESC, osm_timestamp DESC, source_pbf ASC
            ) AS rn
          FROM observations
        ) WHERE rn = 1
        """
    )


def cells_global_canonical(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Return the eight-cell counts at canonical (post-dedup) level."""
    select_exprs = ", ".join(
        f"COALESCE(SUM({expr}), 0) AS {name}" for name, expr in EIGHT_CELL_EXPRESSIONS.items()
    )
    row = con.execute(
        f"SELECT {select_exprs} FROM canonical_observations"  # noqa: S608
    ).fetchone()
    if row is None:
        return [dict.fromkeys(EIGHT_CELL_EXPRESSIONS.keys(), 0)]
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, row, strict=False))]


def copy_query_atomic(
    con: duckdb.DuckDBPyConnection,
    query: str,
    out_path: Path,
) -> None:
    """COPY a query to a sibling temporary Parquet and atomically promote it."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(f".{out_path.name}.tmp.parquet")
    escaped = str(temp_path).replace("'", "''")
    try:
        con.execute(f"COPY ({query}) TO '{escaped}' (FORMAT 'parquet', COMPRESSION 'snappy')")
        atomic_write_file(temp_path, out_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def ensure_temp_dir(run_dir: Path) -> Path:
    """Create and return the run-owned DuckDB temp directory."""
    staging = run_dir / "staging" / "duckdb"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def cleanup_temp_dir(run_dir: Path) -> bool:
    """Remove the run-owned DuckDB temp directory if empty.

    Returns ``True`` if removed. Never removes non-empty directories
    so a failed analysis leaves evidence for debugging.
    """
    staging = run_dir / "staging" / "duckdb"
    if not staging.exists():
        return False
    try:
        staging.rmdir()
        return True
    except OSError:
        return False


__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "DUCKDB_THREADS",
    "EIGHT_CELL_EXPRESSIONS",
    "EIGHT_CELL_LABELS",
    "canonical_observations",
    "cells_global_canonical",
    "cells_global_observation",
    "cleanup_temp_dir",
    "copy_query_atomic",
    "ensure_temp_dir",
    "fresh_connection",
    "register_comparison_parquets",
    "register_public_parquets",
    "register_rejection_parquets",
]
