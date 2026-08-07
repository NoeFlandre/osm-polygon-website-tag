"""Bounded, deterministic coordinate input from public Parquet shards."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq

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
        parquet = pq.ParquetFile(path)
        names = set(parquet.schema_arrow.names)
        missing = {"lat", "lon"} - names
        if missing:
            raise ValueError(f"missing coordinate columns {sorted(missing)} in {path}")
        if extracted_text_only and not set(_TEXT_STATUS_COLUMNS).issubset(names):
            continue
        columns = ["lat", "lon", *_TEXT_STATUS_COLUMNS] if extracted_text_only else ["lat", "lon"]
        offset = 0
        for batch in parquet.iter_batches(columns=columns, batch_size=8192):
            latitudes = batch.column("lat")
            longitudes = batch.column("lon")
            lat_values = latitudes.to_numpy(zero_copy_only=False)
            lon_values = longitudes.to_numpy(zero_copy_only=False)
            lat_nulls = latitudes.is_null().to_numpy(zero_copy_only=False)
            lon_nulls = longitudes.is_null().to_numpy(zero_copy_only=False)
            text_values: Any | None = None
            if extracted_text_only:
                website_success = _arrow_kernel(
                    "equal", batch.column("website_text_status"), "success"
                )
                contact_success = _arrow_kernel(
                    "equal", batch.column("contact_website_text_status"), "success"
                )
                text_values = pc.fill_null(
                    _arrow_kernel("or_kleene", website_success, contact_success), False
                ).to_numpy(zero_copy_only=False)
            for local_index, (lat, lon, lat_is_null, lon_is_null) in enumerate(
                zip(lat_values, lon_values, lat_nulls, lon_nulls, strict=True)
            ):
                if text_values is not None and not bool(text_values[local_index]):
                    continue
                index = offset + local_index
                if lat_is_null or lon_is_null:
                    raise ValueError(f"null coordinate in {path.name} row {index}")
                yield path, index, float(lat), float(lon)
            offset += batch.num_rows


def _arrow_kernel(name: str, *args: Any) -> Any:
    """Call a dynamically registered Arrow kernel while keeping ty strict."""
    return pc.call_function(name, list(args))
