"""Bounded, deterministic coordinate input from public Parquet shards."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyarrow.parquet as pq


def sorted_public_polygon_parquets(run_dir: Path | str) -> list[Path]:
    """Return public polygon shards in deterministic path order."""
    return sorted((Path(run_dir) / "polygons").glob("*.parquet"))


def iter_lat_lon_runs(run_dir: Path | str) -> Iterator[tuple[Path, int, float, float]]:
    """Yield ``(path, row_index, lat, lon)`` using only coordinate columns."""
    for path in sorted_public_polygon_parquets(run_dir):
        parquet = pq.ParquetFile(path)
        names = set(parquet.schema_arrow.names)
        missing = {"lat", "lon"} - names
        if missing:
            raise ValueError(f"missing coordinate columns {sorted(missing)} in {path}")
        offset = 0
        for batch in parquet.iter_batches(columns=["lat", "lon"], batch_size=8192):
            latitudes = batch.column("lat").to_pylist()
            longitudes = batch.column("lon").to_pylist()
            for index, (lat, lon) in enumerate(zip(latitudes, longitudes, strict=True), offset):
                if lat is None or lon is None:
                    raise ValueError(f"null coordinate in {path.name} row {index}")
                yield path, index, float(lat), float(lon)
            offset += batch.num_rows
