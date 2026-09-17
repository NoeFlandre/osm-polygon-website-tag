"""Bounded, deterministic coordinate input from public Parquet shards."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.arrow import call_arrow_kernel

_TEXT_STATUS_COLUMNS = ("website_text_status", "contact_website_text_status")
_TEXT_COLUMNS = (
    "website_text",
    "website_text_status",
    "contact_website_text",
    "contact_website_text_status",
)
_IDENTITY_COLUMNS = ("osm_type", "osm_id")


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


def iter_unique_text_lat_lon_runs(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> Iterator[tuple[Path, int, float, float]]:
    """Yield one centroid per globally unique OSM polygon with non-empty text.

    Regional copies are read in deterministic path and row order. An identity
    is reserved only after a row has successful, trimmed non-empty website or
    contact:website text, so a failed or empty copy cannot hide a later
    successful copy. The first qualifying copy supplies the centroid.
    """
    seen: set[tuple[str, int]] = set()
    for path in _text_polygon_parquets(run_dir, source_names=source_names):
        yield from _iter_unique_text_path_rows(path, seen)


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


def _iter_unique_text_path_rows(
    path: Path,
    seen: set[tuple[str, int]],
) -> Iterator[tuple[Path, int, float, float]]:
    """Yield qualifying rows from one shard while updating global identities."""
    parquet = pq.ParquetFile(path)
    names = set(parquet.schema_arrow.names)
    columns = _path_columns(path, names, extracted_text_only=True, include_identity=True)
    if columns is None:
        return
    offset = 0
    for batch in parquet.iter_batches(columns=columns, batch_size=8192):
        yield from _iter_unique_text_batch_rows(path, batch, offset, seen)
        offset += batch.num_rows


def _iter_unique_text_batch_rows(
    path: Path,
    batch: Any,
    offset: int,
    seen: set[tuple[str, int]],
) -> Iterator[tuple[Path, int, float, float]]:
    """Select text rows and reserve each qualifying OSM identity once."""
    latitudes = batch.column("lat")
    longitudes = batch.column("lon")
    lat_values = latitudes.to_numpy(zero_copy_only=False)
    lon_values = longitudes.to_numpy(zero_copy_only=False)
    lat_nulls = latitudes.is_null().to_numpy(zero_copy_only=False)
    lon_nulls = longitudes.is_null().to_numpy(zero_copy_only=False)
    text_values = _text_success_mask(batch).tolist()
    osm_types = batch.column("osm_type").to_pylist()
    osm_ids = batch.column("osm_id").to_pylist()
    for local_index, (lat, lon, lat_is_null, lon_is_null) in enumerate(
        zip(lat_values, lon_values, lat_nulls, lon_nulls, strict=True)
    ):
        if not _reserve_text_identity(
            text_values,
            local_index,
            osm_types[local_index],
            osm_ids[local_index],
            seen,
        ):
            continue
        yield _coordinate_value(
            path,
            offset + local_index,
            lat,
            lon,
            lat_is_null=lat_is_null,
            lon_is_null=lon_is_null,
        )


def _path_columns(
    path: Path,
    names: set[str],
    *,
    extracted_text_only: bool,
    include_identity: bool = False,
) -> list[str] | None:
    """Validate required shard columns and select the bounded read set."""
    missing = {"lat", "lon"} - names
    if missing:
        raise ValueError(f"missing coordinate columns {sorted(missing)} in {path}")
    if not extracted_text_only:
        return ["lat", "lon"]
    return _text_path_columns(names, include_identity=include_identity)


def _text_path_columns(names: set[str], *, include_identity: bool) -> list[str] | None:
    """Return text columns when a shard proves the requested text contract."""
    if not set(_TEXT_COLUMNS).issubset(names):
        return None
    if include_identity and not set(_IDENTITY_COLUMNS).issubset(names):
        return None
    identity = list(_IDENTITY_COLUMNS) if include_identity else []
    return [*identity, "lat", "lon", *_TEXT_COLUMNS]


def _reserve_text_identity(
    text_values: Any,
    index: int,
    osm_type: Any,
    osm_id: Any,
    seen: set[tuple[str, int]],
) -> bool:
    """Reserve one qualifying OSM identity and report whether it is new."""
    if not _row_is_eligible(text_values, index):
        return False
    if osm_type is None or osm_id is None:
        return False
    identity = (str(osm_type), int(osm_id))
    if identity in seen:
        return False
    seen.add(identity)
    return True


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
    """Return a null-safe mask for trimmed, non-empty successful text."""
    website_success = _successful_non_empty_text(
        batch.column("website_text"), batch.column("website_text_status")
    )
    contact_success = _successful_non_empty_text(
        batch.column("contact_website_text"), batch.column("contact_website_text_status")
    )
    return pc.fill_null(
        call_arrow_kernel("or_kleene", website_success, contact_success), False
    ).to_numpy(zero_copy_only=False)


def _successful_non_empty_text(text: Any, status: Any) -> Any:
    """Return a mask for one text field with the success status."""
    trimmed = call_arrow_kernel("utf8_trim_whitespace", text)
    non_empty = call_arrow_kernel("not_equal", trimmed, "")
    successful = call_arrow_kernel("equal", status, "success")
    return call_arrow_kernel("and_kleene", successful, non_empty)


def _text_polygon_parquets(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None,
) -> list[Path]:
    """Return the regional text sources when a canonical run retains them."""
    root = Path(run_dir)
    observations_dir = root / "analysis_observations"
    if observations_dir.is_symlink():
        try:
            regional_polygons = observations_dir.resolve().parent / "polygons"
        except OSError:
            regional_polygons = None
        if regional_polygons is not None and regional_polygons.is_dir():
            return _select_polygon_parquets(regional_polygons, source_names)
    return sorted_public_polygon_parquets(root, source_names=source_names)


def _select_polygon_parquets(
    directory: Path,
    source_names: Collection[str] | None,
) -> list[Path]:
    """Select deterministic polygon paths from an arbitrary polygon directory."""
    paths = sorted(directory.glob("*.parquet"))
    if source_names is None:
        return paths
    stems = {name.removesuffix(".osm.pbf") for name in source_names}
    return [path for path in paths if path.stem in stems]
