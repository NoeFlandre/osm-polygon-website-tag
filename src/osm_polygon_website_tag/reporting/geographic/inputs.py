"""Bounded, deterministic coordinate input from public Parquet shards."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.arrow import call_arrow_kernel

_TEXT_STATUS_COLUMNS = ("website_text_status", "contact_website_text_status")


def sorted_public_polygon_parquets(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> list[Path]:
    """Return public polygon shards in deterministic path order.

    When ``source_names`` is supplied, only those source-scoped shards are
    returned. This keeps progress cards and maps aligned with the uploaded
    remote snapshot instead of every local extraction artifact.
    """
    paths = sorted((Path(run_dir) / "polygons").glob("*.parquet"))
    if source_names is None:
        return paths
    stems = {name.removesuffix(".osm.pbf") for name in source_names}
    return [path for path in paths if path.stem in stems]


def iter_lat_lon_runs(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
    extracted_text_only: bool = False,
) -> Iterator[tuple[Path, int, float, float]]:
    """Yield ``(path, row_index, lat, lon)`` from selected public rows.

    When ``extracted_text_only`` is true, a row is yielded only when either
    website text status is ``"success"``. Shards without the text-status
    columns are skipped because they cannot prove that text was extracted.
    """
    for path in sorted_public_polygon_parquets(run_dir, source_names=source_names):
        yield from _iter_path_rows(path, extracted_text_only=extracted_text_only)


def _iter_path_rows(
    path: Path, *, extracted_text_only: bool
) -> Iterator[tuple[Path, int, float, float]]:
    """Yield valid coordinate rows from one Parquet shard."""
    parquet = pq.ParquetFile(path)
    names = set(parquet.schema_arrow.names)
    columns = _path_columns(path, names, extracted_text_only=extracted_text_only)
    if columns is None:
        return
    offset = 0
    for batch in parquet.iter_batches(columns=columns, batch_size=8192):
        yield from _iter_batch_rows(path, batch, offset, extracted_text_only=extracted_text_only)
        offset += batch.num_rows


def _path_columns(path: Path, names: set[str], *, extracted_text_only: bool) -> list[str] | None:
    """Validate required shard columns and select the bounded read set."""
    missing = {"lat", "lon"} - names
    if missing:
        raise ValueError(f"missing coordinate columns {sorted(missing)} in {path}")
    if extracted_text_only and not set(_TEXT_STATUS_COLUMNS).issubset(names):
        return None
    return ["lat", "lon", *_TEXT_STATUS_COLUMNS] if extracted_text_only else ["lat", "lon"]


def _iter_batch_rows(
    path: Path,
    batch: Any,
    offset: int,
    *,
    extracted_text_only: bool,
) -> Iterator[tuple[Path, int, float, float]]:
    """Yield validated coordinates from one bounded Arrow batch."""
    latitudes = batch.column("lat")
    longitudes = batch.column("lon")
    lat_values = latitudes.to_numpy(zero_copy_only=False)
    lon_values = longitudes.to_numpy(zero_copy_only=False)
    lat_nulls = latitudes.is_null().to_numpy(zero_copy_only=False)
    lon_nulls = longitudes.is_null().to_numpy(zero_copy_only=False)
    text_values = _text_success_mask(batch) if extracted_text_only else None
    yield from _validated_coordinates(
        path,
        zip(lat_values, lon_values, lat_nulls, lon_nulls, strict=True),
        text_values,
        offset,
    )


def _validated_coordinates(
    path: Path,
    values: Any,
    text_values: Any,
    offset: int,
) -> Iterator[tuple[Path, int, float, float]]:
    """Validate and yield one batch's coordinate tuples."""
    for local_index, (lat, lon, lat_is_null, lon_is_null) in enumerate(values):
        if not _row_is_eligible(text_values, local_index):
            continue
        yield _coordinate_value(
            path,
            offset + local_index,
            lat,
            lon,
            lat_is_null=lat_is_null,
            lon_is_null=lon_is_null,
        )


def _row_is_eligible(text_values: Any, index: int) -> bool:
    """Return whether a row passes the optional extracted-text mask."""
    return text_values is None or bool(text_values[index])


def _coordinate_value(
    path: Path,
    index: int,
    lat: Any,
    lon: Any,
    *,
    lat_is_null: bool,
    lon_is_null: bool,
) -> tuple[Path, int, float, float]:
    """Validate one coordinate pair and convert it to plain Python values."""
    if lat_is_null or lon_is_null:
        raise ValueError(f"null coordinate in {path.name} row {index}")
    return path, index, float(lat), float(lon)


def _text_success_mask(batch: Any) -> Any:
    """Return a null-safe mask for rows with text in either field."""
    website_success = call_arrow_kernel("equal", batch.column("website_text_status"), "success")
    contact_success = call_arrow_kernel(
        "equal", batch.column("contact_website_text_status"), "success"
    )
    return pc.fill_null(
        call_arrow_kernel("or_kleene", website_success, contact_success), False
    ).to_numpy(zero_copy_only=False)
