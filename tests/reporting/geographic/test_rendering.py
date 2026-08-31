"""RED tests for deterministic H3 density-map rendering."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pyarrow as pa
import pyarrow.parquet as pq
from matplotlib.figure import Figure

from osm_polygon_website_tag.reporting.geographic.basemap import (
    _draw_feature,
    _draw_multipolygon_feature,
    _draw_polygon,
    _draw_polygon_feature,
)
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary
from osm_polygon_website_tag.reporting.geographic.polygon_density import (
    build_polygon_density_map,
)


def test_bundled_land_backdrop_is_present() -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    assert rendering.BUNDLED_LAND_PATH.is_file()


def test_renderer_draws_reference_land_backdrop(tmp_path: Path, monkeypatch) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering
    from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary

    calls: list[Path] = []
    monkeypatch.setattr(
        rendering,
        "draw_landmasses",
        lambda _axis, path: calls.append(path),
    )

    rendering.render_polygon_density(
        PolygonDensitySummary(3, 0, 0, ()),
        tmp_path / "map.png",
    )

    assert calls == [rendering.BUNDLED_LAND_PATH]


def test_renderer_builds_caption_and_saves_nonempty_map_without_encoding_png(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Rendering decisions are tested without repeatedly encoding a full PNG."""
    from osm_polygon_website_tag.reporting.geographic import rendering

    output = tmp_path / "map.png"
    saved: list[tuple[Figure, Path]] = []
    land_calls: list[object] = []
    monkeypatch.setattr(rendering, "draw_landmasses", lambda axis, _path: land_calls.append(axis))
    monkeypatch.setattr(
        rendering,
        "cell_boundary_rings",
        lambda _cell: [[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, -1.0)]],
    )
    monkeypatch.setattr(
        rendering,
        "atomic_save_png",
        lambda figure, path: saved.append((figure, path)),
    )

    caption = rendering.render_polygon_density(
        PolygonDensitySummary(
            h3_resolution=5,
            polygon_row_count=3,
            occupied_cell_count=1,
            cells=(("85283473fffffff", 3),),
        ),
        output,
    )

    assert "H3 resolution 5" in caption
    assert "3 polygon centroids" in caption
    assert land_calls and saved and saved[0][1] == output
    assert len(saved[0][0].axes[0].patches) == 1


def test_renderer_empty_summary_uses_explanatory_label_and_still_saves(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    saved: list[Path] = []
    monkeypatch.setattr(rendering, "draw_landmasses", lambda *_args: None)
    monkeypatch.setattr(rendering, "atomic_save_png", lambda _figure, path: saved.append(path))

    caption = rendering.render_polygon_density(
        PolygonDensitySummary(5, 0, 0, ()),
        tmp_path / "empty.png",
    )

    assert "0 occupied cells" in caption
    assert saved == [tmp_path / "empty.png"]


def test_atomic_save_png_replaces_temporary_file(tmp_path: Path) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    output = tmp_path / "map.png"
    temporary_paths: list[Path] = []

    class Figure:
        def savefig(self, path: Path, **_kwargs: object) -> None:
            temporary_paths.append(path)
            path.write_bytes(b"png")

    rendering.atomic_save_png(Figure(), output)

    assert output.read_bytes() == b"png"
    assert temporary_paths and temporary_paths[0].parent == output.parent
    assert not temporary_paths[0].exists()


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


def test_basemap_private_draw_helpers_handle_polygon_shapes() -> None:
    figure, axis = plt.subplots()
    try:
        polygon = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]
        _draw_polygon(axis, polygon)
        _draw_polygon(axis, [])
        _draw_polygon_feature(axis, polygon)
        _draw_polygon_feature(axis, [])
        _draw_multipolygon_feature(axis, [polygon, polygon])
        _draw_multipolygon_feature(axis, [])
        _draw_feature(axis, {"geometry": {"type": "Polygon", "coordinates": polygon}})
        _draw_feature(axis, {"geometry": {"type": "MultiPolygon", "coordinates": [polygon]}})
        _draw_feature(axis, {"geometry": {"type": "LineString", "coordinates": []}})
        assert len(axis.patches) == 6
    finally:
        plt.close(figure)
