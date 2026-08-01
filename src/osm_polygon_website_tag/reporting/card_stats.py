"""Recompute every numeric statistic the README card displays.

Every number is derived from the published artifacts in the run
directory. The card builder calls :func:`compute_card_stats` once and
injects the result into the README template; the card builder does not
otherwise compute anything.

Outputs are returned as :class:`CardStats` dataclass for ergonomic
use from the card builder.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.geographic.aggregation import (
    compute_polygon_density_summary,
)
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary


@dataclass
class CardStats:
    """All numeric statistics rendered on the README card."""

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
    density = summary or compute_polygon_density_summary(run_dir, source_names=source_names)
    stats.polygon_density_h3_resolution = density.h3_resolution
    stats.occupied_h3_cell_count = density.occupied_cell_count
    stats.polygon_density_row_count = density.polygon_row_count

    polygons_dir = run_dir / "polygons"
    obs_dir = run_dir / "analysis_observations"
    rej_dir = run_dir / "rejections"
    analysis_dir = run_dir / "analysis"
    for d in (polygons_dir, obs_dir, rej_dir):
        if not d.exists():
            raise FileNotFoundError(f"missing {d}")

    public_shards = _selected_parquets(polygons_dir, source_names)
    observation_shards = _selected_parquets(obs_dir, source_names)
    rejection_shards = _selected_parquets(rej_dir, source_names)
    stats.public_row_count = _count_parquets(public_shards)
    stats.observation_count = _count_parquets(observation_shards)
    stats.rejection_count = _count_parquets(rejection_shards)
    stats.sources_count = len(public_shards)
    expected_path = run_dir / "manifests" / "expected_sources.json"
    if expected_path.is_file():
        expected = json.loads(expected_path.read_text())
        stats.expected_sources_count = len(expected)
    else:
        stats.expected_sources_count = stats.sources_count

    for shard in public_shards:
        _add_text_stats(stats, shard)
        stats.per_source_counts.append(
            {
                "source_pbf": f"{shard.stem}.osm.pbf",
                "row_count": int(pq.ParquetFile(shard).metadata.num_rows),
            }
        )

    if source_names is None and analysis_dir.exists():
        dup_path = analysis_dir / "duplicate_observations.parquet"
        if dup_path.exists():
            stats.duplicate_count = int(pq.ParquetFile(dup_path).metadata.num_rows)
        conf_path = analysis_dir / "conflicting_snapshots.parquet"
        if conf_path.exists():
            stats.conflicting_snapshot_count = int(pq.ParquetFile(conf_path).metadata.num_rows)
        cg_path = analysis_dir / "cells_global.parquet"
        if cg_path.exists():
            table = pq.read_table(cg_path).to_pylist()
            for row in table:
                level = row.get("level")
                cell = row.get("cell")
                count = int(row.get("row_count", 0))
                if level == "observation":
                    stats.eight_cell_observation[cell] = count
                else:
                    stats.eight_cell_canonical[cell] = count
            stats.canonical_count = sum(stats.eight_cell_canonical.values())
        for src_table, dst_attr, _key in (
            ("top_hostnames_website.parquet", "top_hostnames_website", "website_hostname"),
            (
                "top_hostnames_contact_website.parquet",
                "top_hostnames_contact_website",
                "contact_website_hostname",
            ),
        ):
            p = analysis_dir / src_table
            if p.exists():
                rows = pq.read_table(p).to_pylist()
                setattr(stats, dst_attr, rows)

    return stats


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
    available = set(parquet.schema_arrow.names)
    required = {
        "website",
        "contact_website",
        "website_word_count",
        "website_text_status",
        "contact_website_word_count",
        "contact_website_text_status",
    }
    if not required.issubset(available):
        return
    has_pending = False
    columns = sorted(required)
    for batch in parquet.iter_batches(columns=columns, batch_size=8_192):
        for row in batch.to_pylist():
            website_status = row["website_text_status"]
            contact_status = row["contact_website_text_status"]
            stats.website_urls_present += int(row["website"] is not None)
            stats.contact_website_urls_present += int(row["contact_website"] is not None)
            stats.website_text_success_count += int(website_status == "success")
            stats.contact_website_text_success_count += int(contact_status == "success")
            stats.website_text_empty_count += int(website_status == "empty")
            stats.contact_website_text_empty_count += int(contact_status == "empty")
            stats.website_text_failure_count += int(
                website_status not in {"absent", "pending", "success", "empty"}
            )
            stats.contact_website_text_failure_count += int(
                contact_status not in {"absent", "pending", "success", "empty"}
            )
            if website_status == "success":
                stats.website_total_words += int(row["website_word_count"])
            if contact_status == "success":
                stats.contact_website_total_words += int(row["contact_website_word_count"])
            stats.polygons_with_any_text += int(
                website_status == "success" or contact_status == "success"
            )
            has_pending = has_pending or website_status == "pending" or contact_status == "pending"
    if not has_pending:
        stats.enriched_sources_count += 1
