"""Build a globally canonical public polygon split from source shards.

The extraction split intentionally preserves one row per source snapshot. This
module derives a separate, lossless-in-input canonical split with at most one
row per ``(osm_type, osm_id)``. The selected row is never merged with another
row: website values, geometry, and extracted text all come from the same
deterministic winner.

Winner order, from strongest to weakest, is:

1. highest OSM version;
2. newest OSM timestamp;
3. lexicographically smallest source PBF filename;
4. lexicographically smallest polygon identifier (final total-order tie-break).

The source split is read-only. Output is built in a sibling temporary
directory and promoted only after every canonical shard has the exact public
schema. All expected source filenames receive a shard, including empty ones.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA, schema_matches

_SOURCE_SUFFIX = ".osm.pbf"


@dataclass(frozen=True)
class DeduplicationSummary:
    """Artifact-derived counts for one canonicalization run."""

    input_row_count: int
    output_row_count: int
    duplicate_group_count: int
    duplicate_extra_row_count: int
    website_conflict_group_count: int
    contact_website_conflict_group_count: int
    source_count: int
    output_counts_by_source: dict[str, int]


def deduplicate_public_shards(
    source_dir: Path | str,
    output_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> DeduplicationSummary:
    """Write one deterministic canonical shard per source PBF.

    ``source_dir`` is never modified. ``output_dir`` must not already exist;
    this fail-closed rule prevents an interrupted or repeated run from
    replacing a previous derivative. Pass ``source_names`` to preserve empty
    expected source shards; otherwise names are derived from input filenames.
    """
    source_dir = Path(source_dir)
    output_dir = Path(output_dir)
    input_paths = sorted(source_dir.glob("*.parquet"))
    if not input_paths:
        raise FileNotFoundError(f"no public Parquet shards under {source_dir}")
    if output_dir.exists():
        raise FileExistsError(f"canonical output already exists: {output_dir}")

    names = _normalise_source_names(source_names, input_paths)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    con: duckdb.DuckDBPyConnection | None = None
    try:
        con = duckdb.connect()
        con.execute("PRAGMA threads=4")
        temp_dir = staging_dir / "duckdb-temp"
        temp_dir.mkdir()
        escaped_temp = str(temp_dir).replace("'", "''")
        con.execute(f"SET temp_directory='{escaped_temp}'")
        glob = str(source_dir / "*.parquet").replace("'", "''")
        con.execute(
            f"""
            CREATE TEMP VIEW source_rows AS
            SELECT * FROM read_parquet('{glob}')
            """  # noqa: S608
        )
        actual_sources = {
            str(row[0])
            for row in con.execute("SELECT DISTINCT source_pbf FROM source_rows").fetchall()
            if row[0] is not None
        }
        unknown_sources = actual_sources - set(names)
        if unknown_sources:
            raise ValueError(
                "source shards contain unlisted source PBFs: " + ", ".join(sorted(unknown_sources))
            )
        con.execute(
            """
            CREATE TEMP VIEW canonical_rows AS
            SELECT * EXCLUDE (row_number) FROM (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY osm_type, osm_id
                ORDER BY osm_version DESC, osm_timestamp DESC, source_pbf ASC, polygon_id ASC
              ) AS row_number
              FROM source_rows
            )
            WHERE row_number = 1
            """
        )

        input_count = _scalar_count(con, "SELECT COUNT(*) FROM source_rows")
        output_count = _scalar_count(con, "SELECT COUNT(*) FROM canonical_rows")
        duplicate_group_count = _scalar_count(
            con,
            """
            SELECT COUNT(*) FROM (
              SELECT osm_type, osm_id FROM source_rows
              GROUP BY osm_type, osm_id HAVING COUNT(*) > 1
            )
            """,
        )
        website_conflict_group_count = _scalar_count(
            con,
            _conflict_query("website"),
        )
        contact_conflict_group_count = _scalar_count(
            con,
            _conflict_query("contact_website"),
        )
        output_counts = {
            str(source): int(count)
            for source, count in con.execute(
                """
                SELECT source_pbf, COUNT(*)::BIGINT
                FROM canonical_rows
                GROUP BY source_pbf
                ORDER BY source_pbf
                """
            ).fetchall()
        }

        partition_dir = staging_dir / "partitions"
        escaped_partition_dir = str(partition_dir).replace("'", "''")
        con.execute(
            f"""
            COPY (
              SELECT * FROM canonical_rows
              ORDER BY osm_type, osm_id
            ) TO '{escaped_partition_dir}'
            (FORMAT PARQUET, COMPRESSION 'snappy', PARTITION_BY (source_pbf),
             WRITE_PARTITION_COLUMNS TRUE)
            """  # noqa: S608
        )
        _materialise_partitions(partition_dir, staging_dir, names)
        for source_name in names:
            destination = staging_dir / _parquet_name(source_name)
            schema = pq.read_schema(destination)
            if not schema_matches(schema, POLYGON_PUBLIC_SCHEMA):
                raise ValueError(f"canonical shard has unexpected schema: {destination}")

        con.close()
        con = None
        _remove_tree(temp_dir)
        staging_dir.rename(output_dir)
        return DeduplicationSummary(
            input_row_count=input_count,
            output_row_count=output_count,
            duplicate_group_count=duplicate_group_count,
            duplicate_extra_row_count=input_count - output_count,
            website_conflict_group_count=website_conflict_group_count,
            contact_website_conflict_group_count=contact_conflict_group_count,
            source_count=len(names),
            output_counts_by_source={source: output_counts.get(source, 0) for source in names},
        )
    finally:
        if con is not None:
            with contextlib.suppress(Exception):
                con.close()
        with contextlib.suppress(OSError):
            _remove_tree(staging_dir)


def _normalise_source_names(
    source_names: Collection[str] | None,
    input_paths: Collection[Path],
) -> tuple[str, ...]:
    if source_names is None:
        names = tuple(f"{path.stem}{_SOURCE_SUFFIX}" for path in input_paths)
    else:
        names = tuple(sorted(source_names))
    if not names or len(set(names)) != len(names):
        raise ValueError("source_names must contain at least one unique source PBF name")
    if any(not name.endswith(_SOURCE_SUFFIX) for name in names):
        raise ValueError("source_names must end with '.osm.pbf'")
    return names


def _parquet_name(source_name: str) -> str:
    return f"{source_name.removesuffix(_SOURCE_SUFFIX)}.parquet"


def _scalar_count(con: duckdb.DuckDBPyConnection, query: str) -> int:
    value = con.execute(query).fetchone()
    return int(value[0]) if value else 0


def _materialise_partitions(
    partition_dir: Path,
    staging_dir: Path,
    source_names: Collection[str],
) -> None:
    """Restore source filenames and the exact Arrow contract after one COPY."""
    for source_name in source_names:
        destination = staging_dir / _parquet_name(source_name)
        parts = sorted((partition_dir / f"source_pbf={source_name}").glob("*.parquet"))
        if not parts:
            table = pa.Table.from_batches([], schema=POLYGON_PUBLIC_SCHEMA)
        else:
            table = pa.concat_tables(
                [pq.read_table(part) for part in parts],
                promote_options="default",
            )
            if table.num_rows > 1:
                table = table.sort_by([("osm_type", "ascending"), ("osm_id", "ascending")])
            table = table.cast(POLYGON_PUBLIC_SCHEMA)
        pq.write_table(table, destination, compression="snappy")
    _remove_tree(partition_dir)


def _conflict_query(column: str) -> str:
    if column not in {"website", "contact_website"}:
        raise ValueError(f"unsupported conflict column: {column}")
    return f"""
        SELECT COUNT(*) FROM (
          SELECT osm_type, osm_id
          FROM source_rows
          GROUP BY osm_type, osm_id
          HAVING COUNT(*) > 1
            AND (
              COUNT(DISTINCT {column})
              + CASE WHEN COUNT(*) FILTER (WHERE {column} IS NULL) > 0 THEN 1 ELSE 0 END
            ) > 1
        )
        """  # noqa: S608


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    path.rmdir()


__all__ = ["DeduplicationSummary", "deduplicate_public_shards"]
