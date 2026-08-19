"""Validation of row-level polygon and observation invariants."""

from __future__ import annotations

from pathlib import Path

import duckdb

_ROW_CONTRACTS: tuple[tuple[str, str, str], ...] = (
    (
        "polygons",
        """has_any_website IS DISTINCT FROM
                     (has_website OR has_contact_website)
                   OR has_website IS DISTINCT FROM
                     (website IS NOT NULL AND trim(website) <> '')
                   OR has_contact_website IS DISTINCT FROM
                     (contact_website IS NOT NULL AND trim(contact_website) <> '')
                   OR NOT has_any_website
                   OR osm_type NOT IN ('way', 'relation')
                   OR NOT isfinite(lat) OR NOT isfinite(lon)
                   OR NOT isfinite(area_m2) OR area_m2 < 0""",
        "public",
    ),
    (
        "analysis_observations",
        """has_any_website IS DISTINCT FROM
                     (has_website OR has_contact_website)
                   OR has_website IS DISTINCT FROM
                     (website IS NOT NULL AND trim(website) <> '')
                   OR has_contact_website IS DISTINCT FROM
                     (contact_website IS NOT NULL AND trim(contact_website) <> '')
                   OR has_wikidata IS DISTINCT FROM
                     (wikidata IS NOT NULL AND trim(wikidata) <> '')
                   OR (NOT has_any_website AND NOT has_wikidata)
                   OR osm_type NOT IN ('way', 'relation')""",
        "comparison",
    ),
    (
        "rejections",
        """has_any_website IS DISTINCT FROM
                     (has_website OR has_contact_website)
                   OR has_website IS DISTINCT FROM
                     (website IS NOT NULL AND trim(website) <> '')
                   OR has_contact_website IS DISTINCT FROM
                     (contact_website IS NOT NULL AND trim(contact_website) <> '')
                   OR osm_type NOT IN ('way', 'relation')
                   OR rejection_kind IS NULL OR rejection_kind = ''""",
        "rejection",
    ),
)


def verify_row_invariants(root: Path, errors: list[str]) -> None:
    """Verify the SQL row contracts over all available shards."""
    con = duckdb.connect(":memory:")
    try:
        for directory, predicate, label in _ROW_CONTRACTS:
            _verify_row_contract(root, con, directory, predicate, label, errors)
    except Exception as exc:
        errors.append(f"row invariant verification failed: {exc}")
    finally:
        con.close()


def _verify_row_contract(
    root: Path,
    con: duckdb.DuckDBPyConnection,
    directory: str,
    predicate: str,
    label: str,
    errors: list[str],
) -> None:
    files = sorted((root / directory).glob("*.parquet"))
    if not files:
        return
    paths = [str(path) for path in files]
    count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet(?) WHERE {predicate}",  # noqa: S608
        [paths],
    ).fetchone()
    if count and int(count[0]) != 0:
        errors.append(f"{label} row invariant violations: {int(count[0])}")
