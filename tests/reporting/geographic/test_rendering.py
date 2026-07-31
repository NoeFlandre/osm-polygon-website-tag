"""RED tests for deterministic H3 density-map rendering."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.reporting.geographic.polygon_density import (
    build_polygon_density_map,
)


def test_map_is_a_deterministic_png(tmp_path: Path) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    pq.write_table(pa.table({"lat": [48.85, 40.7], "lon": [2.35, -74.0]}), polygons / "a.parquet")

    output = tmp_path / "assets" / "map.png"
    first = build_polygon_density_map(tmp_path, output_path=output)
    first_bytes = output.read_bytes()
    build_polygon_density_map(tmp_path, output_path=output)

    assert first.occupied_cell_count == 2
    assert first_bytes == output.read_bytes()
    assert first_bytes.startswith(b"\x89PNG\r\n\x1a\n")
