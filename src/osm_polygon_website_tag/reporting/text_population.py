"""Deterministic, globally deduplicated website-text populations.

The regional extraction keeps one observation per source shard. This module
reduces those observations with the same winner order used by the canonical
pipeline, using DuckDB's run-owned spill directory so the reducer does not
materialize the dataset in Python.
"""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.artifact_inventory import hash_file
from osm_polygon_website_tag.storage.duckdb_engine import reporting_connection

_REQUIRED_COLUMNS = frozenset(
    {
        "lat",
        "lon",
        "osm_type",
        "osm_id",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    }
)
_OPTIONAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("osm_version", "BIGINT"),
    ("osm_timestamp", "TIMESTAMP"),
    ("source_pbf", "VARCHAR"),
    ("polygon_id", "VARCHAR"),
    ("website", "VARCHAR"),
    ("contact_website", "VARCHAR"),
    ("website_word_count", "BIGINT"),
    ("contact_website_word_count", "BIGINT"),
    ("website_language", "VARCHAR"),
    ("contact_website_language", "VARCHAR"),
)
_BATCH_ROWS = 8_192
_TEXT_COLUMNS = ("website_text", "contact_website_text")
_PREFIX_SQL = """
    osm_version DESC NULLS LAST,
    osm_timestamp DESC NULLS LAST,
    source_pbf ASC NULLS LAST,
    polygon_id ASC NULLS LAST,
    lat ASC NULLS LAST,
    lon ASC NULLS LAST
"""
_TAIL_SQL = """
    website ASC NULLS LAST,
    contact_website ASC NULLS LAST,
    website_word_count DESC NULLS LAST,
    contact_website_word_count DESC NULLS LAST,
    website_language ASC NULLS LAST,
    contact_website_language ASC NULLS LAST,
    __source_path ASC NULLS LAST
"""
_NARROW_ORDER_SQL = f"{_PREFIX_SQL},{_TAIL_SQL}"
# Text qualifies when it holds one character that is neither ASCII nor Unicode
# whitespace. The match stops at that character, where rewriting the whole text
# to test the same thing reads every extracted page in full.
_QUALIFIES_SQL = r"""
          (
            website_text_status = 'success'
            AND REGEXP_MATCHES(COALESCE(website_text, ''), '[^\s\pZ]')
          ) AS website_qualifies,
          (
            contact_website_text_status = 'success'
            AND REGEXP_MATCHES(COALESCE(contact_website_text, ''), '[^\s\pZ]')
          ) AS contact_website_qualifies
"""
_ORDER_SQL = """
    osm_version DESC NULLS LAST,
    osm_timestamp DESC NULLS LAST,
    source_pbf ASC NULLS LAST,
    polygon_id ASC NULLS LAST,
    lat ASC NULLS LAST,
    lon ASC NULLS LAST,
    website_text ASC NULLS LAST,
    contact_website_text ASC NULLS LAST,
    website ASC NULLS LAST,
    contact_website ASC NULLS LAST,
    website_word_count DESC NULLS LAST,
    contact_website_word_count DESC NULLS LAST,
    website_language ASC NULLS LAST,
    contact_website_language ASC NULLS LAST,
    __source_path ASC NULLS LAST
"""


@dataclass(frozen=True)
class TextCoordinate:
    """One canonical qualifying identity's coordinate."""

    osm_type: str
    osm_id: int
    lat: float
    lon: float
    source_path: Path
    row_index: int = 0


@dataclass(frozen=True)
class TextPopulationSummary:
    """Globally deduplicated counts for successful non-empty website text."""

    unique_identity_count: int = 0
    website_identity_count: int = 0
    contact_website_identity_count: int = 0
    website_total_words: int = 0
    contact_website_total_words: int = 0
    website_urls_present: int = 0
    contact_website_urls_present: int = 0
    website_empty_identity_count: int = 0
    contact_website_empty_identity_count: int = 0
    website_failure_identity_count: int = 0
    contact_website_failure_identity_count: int = 0
    website_language_count: int = 0
    contact_website_language_count: int = 0
    detected_language_count: int = 0
    top_languages: tuple[tuple[str, int], ...] = ()


def compute_text_population_summary(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> TextPopulationSummary:
    """Compute unique text populations from the selected regional copies."""
    root = Path(run_dir)
    paths = text_population_parquets(root, source_names=source_names)
    if not paths:
        return TextPopulationSummary()
    with _canonical_connection(root, paths) as connection:
        return _summary_from_connection(connection)


def iter_canonical_text_coordinates(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> Iterator[TextCoordinate]:
    """Yield canonical qualifying coordinates in identity order."""
    root = Path(run_dir)
    paths = text_population_parquets(root, source_names=source_names)
    if not paths:
        return
    with _canonical_connection(root, paths) as connection:
        reader = connection.execute(
            """
            SELECT osm_type, osm_id, lat, lon, __source_path
            FROM canonical_any
            ORDER BY osm_type, osm_id
            """
        ).to_arrow_reader(batch_size=_BATCH_ROWS)
        row_index = 0
        for batch in reader:
            for row in batch.to_pylist():
                yield TextCoordinate(
                    osm_type=str(row["osm_type"]),
                    osm_id=int(row["osm_id"]),
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                    source_path=Path(str(row["__source_path"])),
                    row_index=row_index,
                )
                row_index += 1


def text_population_parquets(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> list[Path]:
    """Return selected shards that prove the global text contract."""
    root = Path(run_dir)
    paths = sorted(_text_population_directory(root).glob("*.parquet"))
    return _contract_paths(_filter_source_paths(paths, source_names))


def text_population_manifest_entries(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> tuple[dict[str, int | str], ...]:
    """Return receipt-bound identities for every selected text shard."""
    entries = [
        {
            "path": f"polygons/{path.name}",
            "size_bytes": path.stat().st_size,
            "sha256": hash_file(path),
        }
        for path in text_population_parquets(run_dir, source_names=source_names)
    ]
    return tuple(sorted(entries, key=lambda item: str(item["path"])))


def _text_population_directory(root: Path) -> Path:
    """Resolve regional polygon shards for canonical runs when available."""
    directory = root / "polygons"
    observations = root / "analysis_observations"
    if not observations.is_symlink():
        return directory
    try:
        regional = observations.resolve().parent / "polygons"
    except OSError:
        return directory
    return regional if regional.is_dir() else directory


def _filter_source_paths(paths: list[Path], source_names: Collection[str] | None) -> list[Path]:
    """Limit shards to the requested source names."""
    if source_names is None:
        return paths
    stems = {name.removesuffix(".osm.pbf") for name in source_names}
    return [path for path in paths if path.stem in stems]


def _contract_paths(paths: Collection[Path]) -> list[Path]:
    """Keep only shards containing the complete text contract."""
    return [path for path in paths if _REQUIRED_COLUMNS.issubset(pq.read_schema(path).names)]


@contextlib.contextmanager
def _canonical_connection(root: Path, paths: Collection[Path]) -> Iterator[Any]:
    """Create canonical DuckDB views over a bounded path selection."""
    connection = reporting_connection(root)
    try:
        _create_views(connection, paths)
        yield connection
    finally:
        connection.close()


def _create_views(connection: Any, paths: Collection[Path]) -> None:
    """Create source, qualifying, and canonical winner views.

    The winner order breaks ties on the extracted text, which for this dataset
    is tens of gigabytes. Carrying it through the ranking sort spills for
    hours, so the population is first reduced to a narrow table that drops the
    text bodies, and the ranking orders by everything else. That is the same
    winner whenever no identity has two rows sharing all of ``_PREFIX_SQL``,
    because the full order decides those rows before it ever reaches the text.
    When such a tie does exist the text-bearing view is restored and the full
    order is used, so the result never depends on the shortcut.
    """
    _create_source_view(connection, paths)
    _create_population_views(connection)
    order = _ORDER_SQL if _has_prefix_ties(connection) else _NARROW_ORDER_SQL
    if order is _ORDER_SQL:
        _restore_text_population_view(connection)
    _create_ranked_view(
        connection,
        "canonical_any",
        "website_qualifies OR contact_website_qualifies",
        order,
    )
    _create_ranked_view(connection, "canonical_website", "website_qualifies", order)
    _create_ranked_view(connection, "canonical_contact", "contact_website_qualifies", order)


def _has_prefix_ties(connection: Any) -> bool:
    """Return whether any identity has two qualifying rows sharing the prefix keys."""
    row = connection.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM all_rows
          WHERE website_qualifies OR contact_website_qualifies
          GROUP BY osm_type, osm_id, osm_version, osm_timestamp, source_pbf, polygon_id, lat, lon
          HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    return bool(row and row[0])


def _restore_text_population_view(connection: Any) -> None:
    """Replace the narrow population with one carrying the extracted text."""
    connection.execute("DROP TABLE all_rows")
    connection.execute(
        f"""
        CREATE TEMP VIEW all_rows AS
        SELECT *, {_QUALIFIES_SQL}
        FROM source_rows
        """  # noqa: S608
    )


def _create_source_view(connection: Any, paths: Collection[Path]) -> None:
    """Create the normalized source view with nullable optional columns."""
    available = set().union(*(set(pq.read_schema(path).names) for path in paths))
    type_by_name = _text_column_types()
    names = tuple(dict.fromkeys((*type_by_name, *(column for column, _ in _OPTIONAL_COLUMNS))))
    selected = [_source_projection(name, available, type_by_name) for name in names]
    selected.append("filename AS __source_path")
    file_list = ", ".join(_sql_string(path) for path in paths)
    connection.execute(
        f"""
        CREATE TEMP VIEW source_rows AS
        SELECT {", ".join(selected)}
        FROM read_parquet([{file_list}], union_by_name=true, filename=true)
        """,  # noqa: S608
    )


def _text_column_types() -> dict[str, str]:
    """Return the normalized types for required and optional text columns."""
    types = dict(_OPTIONAL_COLUMNS)
    types.update(
        {
            "lat": "DOUBLE",
            "lon": "DOUBLE",
            "osm_type": "VARCHAR",
            "osm_id": "BIGINT",
            "website_text": "VARCHAR",
            "website_text_status": "VARCHAR",
            "contact_website_text": "VARCHAR",
            "contact_website_text_status": "VARCHAR",
        }
    )
    return types


def _source_projection(name: str, available: set[str], types: dict[str, str]) -> str:
    """Return one normalized DuckDB projection expression."""
    sql_type = types[name]
    if name in available:
        return f"CAST({name} AS {sql_type}) AS {name}"
    return f"CAST(NULL AS {sql_type}) AS {name}"


def _create_population_views(connection: Any) -> None:
    """Reduce the source rows to the narrow population the ranking sorts.

    One streaming pass reads the extracted text to decide what qualifies and
    then drops it, so the ranking never sorts or spills the text bodies.
    """
    connection.execute(
        f"""
        CREATE TEMP TABLE all_rows AS
        SELECT * EXCLUDE ({", ".join(_TEXT_COLUMNS)}),
        {_QUALIFIES_SQL}
        FROM source_rows
        """  # noqa: S608
    )
    connection.execute(
        """
        CREATE TEMP VIEW qualified_rows AS
        SELECT * FROM all_rows
        WHERE website_qualifies OR contact_website_qualifies
        """
    )


def _create_ranked_view(connection: Any, name: str, predicate: str, order: str) -> None:
    """Create one deterministic winner view for a qualifying population."""
    connection.execute(
        f"""
        CREATE TEMP VIEW {name} AS
        SELECT * EXCLUDE (winner_rank)
        FROM (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY osm_type, osm_id
            ORDER BY {order}
          ) AS winner_rank
          FROM all_rows
          WHERE {predicate}
        )
        WHERE winner_rank = 1
        """  # noqa: S608
    )


def _summary_from_connection(connection: Any) -> TextPopulationSummary:
    """Read scalar and language summaries from canonical winner views."""
    unique, website, contact, website_words, contact_words = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM canonical_any),
          (SELECT COUNT(*) FROM canonical_website),
          (SELECT COUNT(*) FROM canonical_contact),
          (SELECT COALESCE(SUM(website_word_count), 0) FROM canonical_website),
          (SELECT COALESCE(SUM(contact_website_word_count), 0) FROM canonical_contact)
        """
    ).fetchone()
    _validate_word_counts(connection)
    website_urls = _identity_count(connection, "website")
    contact_urls = _identity_count(connection, "contact_website")
    website_empty, contact_empty, website_failure, contact_failure = _status_counts(connection)
    language_rows = connection.execute(
        """
        SELECT language, SUM(row_count)::BIGINT AS row_count
        FROM (
          SELECT website_language AS language, COUNT(*)::BIGINT AS row_count
          FROM canonical_website
          WHERE website_language IS NOT NULL
          GROUP BY website_language
          UNION ALL
          SELECT contact_website_language AS language, COUNT(*)::BIGINT AS row_count
          FROM canonical_contact
          WHERE contact_website_language IS NOT NULL
          GROUP BY contact_website_language
        )
        GROUP BY language
        ORDER BY row_count DESC, language ASC
        """
    ).fetchall()
    website_language_count = _scalar(
        connection,
        "SELECT COUNT(*) FROM canonical_website WHERE website_language IS NOT NULL",
    )
    contact_language_count = _scalar(
        connection,
        "SELECT COUNT(*) FROM canonical_contact WHERE contact_website_language IS NOT NULL",
    )
    return TextPopulationSummary(
        unique_identity_count=int(unique),
        website_identity_count=int(website),
        contact_website_identity_count=int(contact),
        website_total_words=int(website_words or 0),
        contact_website_total_words=int(contact_words or 0),
        website_urls_present=website_urls,
        contact_website_urls_present=contact_urls,
        website_empty_identity_count=website_empty,
        contact_website_empty_identity_count=contact_empty,
        website_failure_identity_count=website_failure,
        contact_website_failure_identity_count=contact_failure,
        website_language_count=website_language_count,
        contact_website_language_count=contact_language_count,
        detected_language_count=len(language_rows),
        top_languages=tuple((str(language), int(count)) for language, count in language_rows),
    )


def _validate_word_counts(connection: Any) -> None:
    """Reject malformed successful text rows instead of silently undercounting."""
    missing = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM canonical_website
        WHERE website_word_count IS NULL
        """,
    ) + _scalar(
        connection,
        """
        SELECT COUNT(*) FROM canonical_contact
        WHERE contact_website_word_count IS NULL
        """,
    )
    if missing:
        raise TypeError("successful text row has no word count")


def _identity_count(connection: Any, tag: str) -> int:
    """Count identities carrying one URL field at least once."""
    column = "website" if tag == "website" else "contact_website"
    return _scalar(
        connection,
        f"""
        SELECT COUNT(*) FROM (
          SELECT DISTINCT osm_type, osm_id
          FROM all_rows
          WHERE {column} IS NOT NULL AND TRIM({column}) <> ''
        )
        """,  # noqa: S608
    )


def _status_counts(connection: Any) -> tuple[int, int, int, int]:
    """Count empty and failed tag populations once per qualifying identity."""
    row = connection.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE NOT website_success AND website_empty AND NOT website_failure),
          COUNT(*) FILTER (
            WHERE NOT contact_success AND contact_empty AND NOT contact_failure
          ),
          COUNT(*) FILTER (WHERE NOT website_success AND website_failure),
          COUNT(*) FILTER (WHERE NOT contact_success AND contact_failure)
        FROM (
          SELECT osm_type, osm_id,
            BOOL_OR(website_qualifies) AS website_success,
            BOOL_OR(contact_website_qualifies) AS contact_success,
            BOOL_OR(website_text_status = 'empty') AS website_empty,
            BOOL_OR(contact_website_text_status = 'empty') AS contact_empty,
            BOOL_OR(
              website_text_status IS NULL
              OR website_text_status NOT IN ('absent', 'pending', 'success', 'empty')
            ) AS website_failure,
            BOOL_OR(
              contact_website_text_status IS NULL
              OR contact_website_text_status NOT IN ('absent', 'pending', 'success', 'empty')
            ) AS contact_failure
          FROM all_rows
          GROUP BY osm_type, osm_id
        )
        """
    ).fetchone()
    website_empty, contact_empty, website_failure, contact_failure = row
    return (
        int(website_empty or 0),
        int(contact_empty or 0),
        int(website_failure or 0),
        int(contact_failure or 0),
    )


def _scalar(connection: Any, query: str) -> int:
    value = connection.execute(query).fetchone()
    return int(value[0]) if value else 0


def _sql_string(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"
