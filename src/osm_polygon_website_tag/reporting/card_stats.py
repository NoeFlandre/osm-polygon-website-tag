"""Recompute every numeric statistic the README card displays.

Every number is derived from the published artifacts in the run
directory. The card builder calls :func:`compute_card_stats` once and
injects the result into the README template; the card builder does not
otherwise compute anything.

The per-shard text totals use bounded Arrow compute kernels over the required
columns, avoiding one Python dictionary allocation per polygon while keeping
the artifact-derived statistics unchanged.

Outputs are returned as :class:`CardStats` dataclass for ergonomic
use from the card builder.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.arrow import call_arrow_kernel
from osm_polygon_website_tag.contracts.text_schema import status_has_retryable_value
from osm_polygon_website_tag.reporting.geographic.aggregation import (
    compute_polygon_density_summary,
)
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary

_TEXT_STATS_COLUMNS = frozenset(
    {
        "website",
        "contact_website",
        "website_word_count",
        "website_text_status",
        "contact_website_word_count",
        "contact_website_text_status",
    }
)


@dataclass
class CardStats:
    """All numeric statistics rendered on the README card."""

    snapshot_status: str | None = None
    observation_count: int = 0
    canonical_count: int = 0
    public_row_count: int = 0
    rejection_count: int = 0
    sources_count: int = 0
    duplicate_count: int = 0
    conflicting_snapshot_count: int = 0
    eight_cell_observation: dict[str, int] = field(default_factory=dict)
    eight_cell_canonical: dict[str, int] = field(default_factory=dict)
    top_hostnames_website: list[dict[str, Any]] = field(default_factory=list)
    top_hostnames_contact_website: list[dict[str, Any]] = field(default_factory=list)
    exact_hostnames_website: list[dict[str, Any]] = field(default_factory=list)
    exact_hostnames_contact_website: list[dict[str, Any]] = field(default_factory=list)
    per_source_counts: list[dict[str, Any]] = field(default_factory=list)
    expected_sources_count: int = 0
    enriched_sources_count: int = 0
    website_urls_present: int = 0
    website_text_success_count: int = 0
    website_text_empty_count: int = 0
    website_text_failure_count: int = 0
    website_total_words: int = 0
    contact_website_urls_present: int = 0
    contact_website_text_success_count: int = 0
    contact_website_text_empty_count: int = 0
    contact_website_text_failure_count: int = 0
    contact_website_total_words: int = 0
    polygons_with_any_text: int = 0
    polygon_density_h3_resolution: int = 3
    occupied_h3_cell_count: int = 0
    polygon_density_row_count: int = 0


def compute_card_stats(
    run_dir: Path | str,
    *,
    summary: PolygonDensitySummary | None = None,
    source_names: Collection[str] | None = None,
) -> CardStats:
    """Recompute every README card statistic from ``run_dir``.

    Reads:

    * ``polygons/*.parquet`` (public)
    * ``analysis_observations/*.parquet`` (comparison)
    * ``rejections/*.parquet``
    * ``analysis/*.parquet`` (analysis tables)

    Returns a :class:`CardStats` with every value derivable from the
    artifacts. Raises :class:`FileNotFoundError` if the run directory
    does not contain the expected subdirectories.
    """
    run_dir = Path(run_dir)
    stats = CardStats()
    stats.snapshot_status = _read_snapshot_status(run_dir)
    _set_density_stats(stats, run_dir, summary=summary, source_names=source_names)
    public_shards, observation_shards, rejection_shards, analysis_dir = _artifact_paths(
        run_dir, source_names=source_names
    )
    _set_shard_counts(stats, public_shards, observation_shards, rejection_shards)
    stats.expected_sources_count = _expected_source_count(run_dir, stats.sources_count)
    _add_public_shard_stats(stats, public_shards)
    _set_unique_polygon_text_count(
        stats,
        _regional_public_shards(
            public_shards,
            observation_shards,
            source_names=source_names,
        ),
    )
    if source_names is None and analysis_dir.exists():
        _add_analysis_stats(stats, analysis_dir)
    return stats


def _set_density_stats(
    stats: CardStats,
    run_dir: Path,
    *,
    summary: PolygonDensitySummary | None,
    source_names: Collection[str] | None,
) -> None:
    """Populate map totals from the extracted-text coordinate summary."""
    density = summary or compute_polygon_density_summary(
        run_dir, source_names=source_names, extracted_text_only=True
    )
    stats.polygon_density_h3_resolution = density.h3_resolution
    stats.occupied_h3_cell_count = density.occupied_cell_count
    stats.polygon_density_row_count = density.polygon_row_count


def _artifact_paths(
    run_dir: Path, *, source_names: Collection[str] | None
) -> tuple[list[Path], list[Path], list[Path], Path]:
    """Resolve and validate the source-scoped artifact directories."""
    directories = tuple(
        run_dir / name for name in ("polygons", "analysis_observations", "rejections")
    )
    for directory in directories:
        if not directory.exists():
            raise FileNotFoundError(f"missing {directory}")
    public, observations, rejections = (
        _selected_parquets(directory, source_names) for directory in directories
    )
    return public, observations, rejections, run_dir / "analysis"


def _set_shard_counts(
    stats: CardStats,
    public_shards: Collection[Path],
    observation_shards: Collection[Path],
    rejection_shards: Collection[Path],
) -> None:
    """Populate row and source counts from selected Parquet metadata."""
    stats.public_row_count = _count_parquets(public_shards)
    stats.observation_count = _count_parquets(observation_shards)
    stats.rejection_count = _count_parquets(rejection_shards)
    stats.sources_count = len(public_shards)


def _expected_source_count(run_dir: Path, fallback: int) -> int:
    """Read expected source count when an inventory manifest is available."""
    expected_path = run_dir / "manifests" / "expected_sources.json"
    if not expected_path.is_file():
        return fallback
    return len(json.loads(expected_path.read_text(encoding="utf-8")))


def _add_public_shard_stats(stats: CardStats, public_shards: Collection[Path]) -> None:
    """Accumulate text totals and per-source row counts."""
    for shard in public_shards:
        _add_text_stats(stats, shard)
        stats.per_source_counts.append(
            {"source_pbf": f"{shard.stem}.osm.pbf", "row_count": _parquet_row_count(shard)}
        )


def _set_unique_polygon_text_count(stats: CardStats, public_shards: Collection[Path]) -> None:
    """Count unique OSM polygons with successful, non-empty extracted text."""
    polygon_ids: set[tuple[str, int]] = set()
    columns = (
        "osm_type",
        "osm_id",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    )
    for shard in public_shards:
        parquet = pq.ParquetFile(shard)
        if not set(columns).issubset(parquet.schema_arrow.names):
            continue
        for batch in parquet.iter_batches(columns=list(columns), batch_size=8_192):
            polygon_ids.update(_text_polygon_ids(batch))
    stats.polygons_with_any_text = len(polygon_ids)


def _regional_public_shards(
    public_shards: Collection[Path],
    observation_shards: Collection[Path],
    *,
    source_names: Collection[str] | None,
) -> list[Path]:
    """Resolve regional public shards for a canonical run when available.

    Canonical runs retain the source-level observation directory as a symlink
    to the regional run. Its sibling ``polygons`` directory contains text
    results from every regional copy, including copies not selected as the
    canonical public row.
    """
    if not observation_shards:
        return list(public_shards)
    observation_shard = next(iter(observation_shards))
    observations_dir = observation_shard.parent
    if not observations_dir.is_symlink():
        return list(public_shards)
    try:
        regional_public_dir = observations_dir.resolve().parent / "polygons"
    except OSError:
        return list(public_shards)
    if not regional_public_dir.is_dir():
        return list(public_shards)
    return _selected_parquets(regional_public_dir, source_names)


def _text_polygon_ids(batch: Any) -> set[tuple[str, int]]:
    """Return qualifying OSM polygon identities from one Arrow batch."""
    website_mask = _non_empty_successful_text_mask(
        batch.column("website_text"), batch.column("website_text_status")
    )
    contact_mask = _non_empty_successful_text_mask(
        batch.column("contact_website_text"), batch.column("contact_website_text_status")
    )
    selected = call_arrow_kernel("or_kleene", website_mask, contact_mask)
    types = call_arrow_kernel("filter", batch.column("osm_type"), selected).to_pylist()
    ids = call_arrow_kernel("filter", batch.column("osm_id"), selected).to_pylist()
    return {
        (str(osm_type), int(osm_id))
        for osm_type, osm_id in zip(types, ids, strict=True)
        if osm_type is not None and osm_id is not None
    }


def _non_empty_successful_text_mask(text: pa.Array, status: pa.Array) -> pa.Array:
    """Return rows whose status is success and whose trimmed text is non-empty."""
    trimmed = call_arrow_kernel("utf8_trim_whitespace", text)
    non_empty = call_arrow_kernel("not_equal", trimmed, "")
    successful = call_arrow_kernel("equal", status, "success")
    return pc.fill_null(call_arrow_kernel("and_kleene", successful, non_empty), False)


def _parquet_row_count(path: Path) -> int:
    """Read a shard row count without materialising its columns."""
    return int(pq.ParquetFile(path).metadata.num_rows)


def _add_analysis_stats(stats: CardStats, analysis_dir: Path) -> None:
    """Load optional duplicate, cell, and hostname analysis tables."""
    stats.duplicate_count = _optional_row_count(analysis_dir / "duplicate_observations.parquet")
    stats.conflicting_snapshot_count = _optional_row_count(
        analysis_dir / "conflicting_snapshots.parquet"
    )
    _add_cell_stats(stats, analysis_dir / "cells_global.parquet")
    _add_hostname_stats(stats, analysis_dir)


def _optional_row_count(path: Path) -> int:
    """Return a table's row count, or zero when it was not published."""
    return _parquet_row_count(path) if path.exists() else 0


def _add_cell_stats(stats: CardStats, path: Path) -> None:
    """Load global H3 counts into their observation/canonical buckets."""
    if not path.exists():
        return
    for row in pq.read_table(path).to_pylist():
        cell = row.get("cell")
        count = int(row.get("row_count", 0))
        if row.get("level") == "observation":
            stats.eight_cell_observation[cell] = count
        else:
            stats.eight_cell_canonical[cell] = count
    stats.canonical_count = sum(stats.eight_cell_canonical.values())


def _add_hostname_stats(stats: CardStats, analysis_dir: Path) -> None:
    """Load optional top-hostname tables into the card statistics."""
    for filename, attribute in (
        ("top_hostnames_website.parquet", "top_hostnames_website"),
        ("top_hostnames_contact_website.parquet", "top_hostnames_contact_website"),
    ):
        path = analysis_dir / filename
        if path.exists():
            setattr(stats, attribute, pq.read_table(path).to_pylist())


def _read_snapshot_status(run_dir: Path) -> str | None:
    """Read the optional user-declared frozen-snapshot marker.

    A ``done`` marker means the owner has intentionally stopped retry work and
    published the current artifacts as the final snapshot. Other values are
    ignored so malformed or unrelated run metadata cannot change the card.
    """
    metadata_path = run_dir / "manifests" / "run.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return (
        "done" if isinstance(metadata, dict) and metadata.get("snapshot_status") == "done" else None
    )


def _selected_parquets(directory: Path, source_names: Collection[str] | None) -> list[Path]:
    paths = sorted(directory.glob("*.parquet"))
    if source_names is None:
        return paths
    stems = {name.removesuffix(".osm.pbf") for name in source_names}
    return [path for path in paths if path.stem in stems]


def _count_parquets(paths: Collection[Path]) -> int:
    return sum(int(pq.ParquetFile(path).metadata.num_rows) for path in paths)


def _add_text_stats(stats: CardStats, shard: Path) -> None:
    parquet = pq.ParquetFile(shard)
    if not _TEXT_STATS_COLUMNS.issubset(parquet.schema_arrow.names):
        return
    has_retryable_status = False
    columns = sorted(_TEXT_STATS_COLUMNS)
    for batch in parquet.iter_batches(columns=columns, batch_size=8_192):
        has_retryable_status = _add_text_batch(stats, batch) or has_retryable_status
    if not has_retryable_status:
        stats.enriched_sources_count += 1


def _add_text_batch(stats: CardStats, batch: Any) -> bool:
    """Accumulate one Arrow batch and report whether it remains retryable."""
    website = batch.column("website")
    website_status = batch.column("website_text_status")
    website_words = batch.column("website_word_count")
    contact_website = batch.column("contact_website")
    contact_status = batch.column("contact_website_text_status")
    contact_words = batch.column("contact_website_word_count")
    website_success = call_arrow_kernel("equal", website_status, "success")
    contact_success = call_arrow_kernel("equal", contact_status, "success")
    _add_url_counts(stats, website, contact_website)
    _add_status_counts(stats, website_status, contact_status, website_success, contact_success)
    stats.website_total_words += _sum_success_words(website_status, website_words)
    stats.contact_website_total_words += _sum_success_words(contact_status, contact_words)
    return status_has_retryable_value(website_status) or status_has_retryable_value(contact_status)


def _add_url_counts(stats: CardStats, website: pa.Array, contact_website: pa.Array) -> None:
    """Count rows carrying each URL field."""
    stats.website_urls_present += _count_true(call_arrow_kernel("is_valid", website))
    stats.contact_website_urls_present += _count_true(
        call_arrow_kernel("is_valid", contact_website)
    )


def _add_status_counts(
    stats: CardStats,
    website_status: pa.Array,
    contact_status: pa.Array,
    website_success: pa.Array,
    contact_success: pa.Array,
) -> None:
    """Accumulate success, empty, and failure counts for both URL fields."""
    stats.website_text_success_count += _count_true(website_success)
    stats.contact_website_text_success_count += _count_true(contact_success)
    stats.website_text_empty_count += _count_true(
        call_arrow_kernel("equal", website_status, "empty")
    )
    stats.contact_website_text_empty_count += _count_true(
        call_arrow_kernel("equal", contact_status, "empty")
    )
    stats.website_text_failure_count += _count_invalid_statuses(website_status)
    stats.contact_website_text_failure_count += _count_invalid_statuses(contact_status)


def _count_true(mask: pa.Array) -> int:
    """Count true values in an Arrow boolean array, ignoring nulls."""
    value = call_arrow_kernel("sum", pc.cast(mask, pa.int64())).as_py()
    return int(value or 0)


def _count_invalid_statuses(status: pa.Array) -> int:
    """Count null or unknown statuses exactly as the former row loop did."""
    known = call_arrow_kernel("equal", status, "absent")
    for expected in ("pending", "success", "empty"):
        known = call_arrow_kernel("or_kleene", known, call_arrow_kernel("equal", status, expected))
    return _count_true(pc.fill_null(call_arrow_kernel("invert", known), True))


def _sum_success_words(status: pa.Array, word_counts: pa.Array) -> int:
    """Sum word counts for successful rows while retaining null failures."""
    selected = call_arrow_kernel(
        "filter", word_counts, call_arrow_kernel("equal", status, "success")
    )
    if selected.null_count:
        raise TypeError("successful text row has no word count")
    value = call_arrow_kernel("sum", selected).as_py()
    return int(value or 0)
