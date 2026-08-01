"""Bounded, deterministic coordinate input from public Parquet shards."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from pathlib import Path

import pyarrow.parquet as pq


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
) -> Iterator[tuple[Path, int, float, float]]:
    """Yield ``(path, row_index, lat, lon)`` using only coordinate columns."""
    for path in sorted_public_polygon_parquets(run_dir, source_names=source_names):
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
