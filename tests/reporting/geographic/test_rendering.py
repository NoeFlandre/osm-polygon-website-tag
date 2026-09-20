"""RED tests for deterministic H3 density-map rendering."""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import matplotlib.pyplot as plt
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from matplotlib.figure import Figure

from osm_polygon_website_tag.reporting.geographic.basemap import (
    _draw_feature,
    _draw_multipolygon_feature,
    _draw_polygon,
    _draw_polygon_feature,
)
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary
from osm_polygon_website_tag.reporting.geographic.polygon_density import (
    build_polygon_density_map,
)


def test_reporting_card_import_defers_matplotlib_until_map_rendering() -> None:
    root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import osm_polygon_website_tag.reporting.card; "
            "print('matplotlib' in sys.modules)",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


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
            extracted_text_only=True,
        ),
        output,
    )

    assert "H3 resolution 5" in caption
    assert "3 unique polygons with extracted text" in caption
    assert land_calls and saved and saved[0][1] == output
    assert len(saved[0][0].axes[0].patches) == 1


def test_renderer_default_caption_describes_regional_rows_and_centroids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    monkeypatch.setattr(rendering, "draw_landmasses", lambda *_args: None)
    monkeypatch.setattr(rendering, "atomic_save_png", lambda *_args: None)

    caption = rendering.render_polygon_density(
        PolygonDensitySummary(5, 3, 1, (("85283473fffffff", 3),)),
        tmp_path / "regional.png",
    )

    assert "3 regional rows/centroids" in caption
    assert "unique polygons" not in caption


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


def test_atomic_save_png_uses_a_same_directory_png_temp_file_and_exact_save_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    output = tmp_path / "deep" / "nested" / "map.png"
    captured: dict[str, object] = {}

    def fake_mkstemp(*, prefix: str, suffix: str, dir: Path) -> tuple[int, str]:
        captured["mkstemp"] = (prefix, suffix, dir)
        temporary = dir / f"{prefix}fixture{suffix}"
        temporary.touch()
        return 42, str(temporary)

    class Figure:
        def savefig(self, path: Path, **kwargs: object) -> None:
            captured["savefig"] = (path, kwargs)
            path.write_bytes(b"png")

    closed: list[int] = []
    monkeypatch.setattr(rendering.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(rendering.os, "close", closed.append)

    rendering.atomic_save_png(Figure(), output)

    assert captured["mkstemp"] == (".map.png.", ".tmp", output.parent)
    temporary, kwargs = cast(tuple[Path, dict[str, object]], captured["savefig"])
    assert Path(temporary).parent == output.parent
    assert kwargs == {
        "format": "png",
        "dpi": 100,
        "facecolor": "white",
        "metadata": {"Software": "osm-polygon-website-tag"},
    }
    assert closed[-1] == 42
    assert output.read_bytes() == b"png"
    assert not Path(temporary).exists()


def test_atomic_save_png_cleans_up_when_figure_save_fails(tmp_path: Path, monkeypatch) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    temporary = tmp_path / ".map.png.fixture.tmp"
    temporary.touch()
    monkeypatch.setattr(
        rendering.tempfile,
        "mkstemp",
        lambda **_kwargs: (42, str(temporary)),
    )
    monkeypatch.setattr(rendering.os, "close", lambda _fd: None)

    class Figure:
        def savefig(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("save failed")

    with pytest.raises(RuntimeError, match="save failed"):
        rendering.atomic_save_png(Figure(), tmp_path / "map.png")
    assert not temporary.exists()


def test_renderer_nonempty_branch_has_an_explicit_deterministic_visual_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    axis_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    figure_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    polygon_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    cmap_calls: list[tuple[object, ...]] = []
    norm_calls: list[dict[str, object]] = []
    scalar_kwargs: list[dict[str, object]] = []
    events: list[tuple[object, ...]] = []
    subplot_calls: list[dict[str, object]] = []

    class Axis:
        def __init__(self) -> None:
            self.transAxes = object()

        def _record(self, name: str, *args: object, **kwargs: object) -> None:
            axis_calls.append((name, args, kwargs))

        def set_facecolor(self, value: object) -> None:
            self._record("set_facecolor", value)

        def set_xlim(self, *values: object) -> None:
            self._record("set_xlim", *values)

        def set_ylim(self, *values: object) -> None:
            self._record("set_ylim", *values)

        def set_xticks(self, values: Iterable[object]) -> None:
            self._record("set_xticks", tuple(values))

        def set_yticks(self, values: Iterable[object]) -> None:
            self._record("set_yticks", tuple(values))

        def set_xlabel(self, value: object) -> None:
            self._record("set_xlabel", value)

        def set_ylabel(self, value: object) -> None:
            self._record("set_ylabel", value)

        def set_title(self, value: object) -> None:
            self._record("set_title", value)

        def grid(self, *args: object, **kwargs: object) -> None:
            self._record("grid", *args, **kwargs)

        def set_aspect(self, *args: object, **kwargs: object) -> None:
            self._record("set_aspect", *args, **kwargs)

        def add_patch(self, patch: object) -> None:
            self._record("add_patch", patch)

        def text(self, *args: object, **kwargs: object) -> None:
            self._record("text", *args, **kwargs)

    class Figure:
        def text(self, *args: object, **kwargs: object) -> None:
            figure_calls.append(("text", args, kwargs))

        def colorbar(self, *args: object, **kwargs: object) -> None:
            figure_calls.append(("colorbar", args, kwargs))

    axis = Axis()
    figure = Figure()
    monkeypatch.setattr(
        rendering.plt,
        "subplots",
        lambda **kwargs: subplot_calls.append(kwargs) or (figure, axis),
    )
    monkeypatch.setattr(rendering, "draw_landmasses", lambda *args: events.append(("land", args)))
    monkeypatch.setattr(
        rendering,
        "cell_boundary_rings",
        lambda cell: [[cell, "end"]],
    )

    class Norm:
        def __call__(self, value: object) -> tuple[str, object]:
            return ("normalized", value)

    norm = Norm()
    monkeypatch.setattr(
        rendering.colors,
        "LogNorm",
        lambda **kwargs: norm_calls.append(kwargs) or norm,
    )

    class Cmap:
        def __call__(self, value: object) -> tuple[str, object]:
            cmap_calls.append((value,))
            return ("color", value)

    cmap = Cmap()
    monkeypatch.setattr(
        rendering.plt, "get_cmap", lambda name: events.append(("cmap", name)) or cmap
    )

    class Polygon:
        def __init__(self, *args: object, **kwargs: object) -> None:
            polygon_calls.append((args, kwargs))

    monkeypatch.setattr(rendering.patches, "Polygon", Polygon)

    class Scalar:
        def __init__(self, **kwargs: object) -> None:
            events.append(("scalar", self))
            scalar_kwargs.append(kwargs)
            self.kwargs = kwargs

        def set_array(self, value: object) -> None:
            events.append(("array", value))

    monkeypatch.setattr(rendering.plt.cm, "ScalarMappable", Scalar)
    monkeypatch.setattr(
        rendering,
        "atomic_save_png",
        lambda fig, path: events.append(("save", fig, path)),
    )
    monkeypatch.setattr(rendering.plt, "close", lambda fig: events.append(("close", fig)))

    caption = rendering.render_polygon_density(
        PolygonDensitySummary(5, 4, 2, (("cell-a", 3), ("cell-b", 1))),
        tmp_path / "map.png",
    )

    assert subplot_calls == [{"figsize": (16, 8), "dpi": 100}]
    assert axis_calls[:9] == [
        ("set_facecolor", ("#cfe2f3",), {}),
        ("set_xlim", (-180, 180), {}),
        ("set_ylim", (-90, 90), {}),
        ("set_xticks", (tuple(range(-180, 181, 30)),), {}),
        ("set_yticks", (tuple(range(-90, 91, 30)),), {}),
        ("set_xlabel", ("Longitude",), {}),
        ("set_ylabel", ("Latitude",), {}),
        ("set_title", ("OSM polygon density by H3 cell (log scale)",), {}),
        (
            "grid",
            (True,),
            {"color": "white", "linewidth": 0.3, "alpha": 0.5},
        ),
    ]
    assert axis_calls[9] == ("set_aspect", ("equal",), {"adjustable": "box"})
    assert norm_calls == [{"vmin": 0.5, "vmax": 3.0}]
    assert scalar_kwargs == [{"norm": norm, "cmap": cmap}]
    assert events[0][0] == "land"
    assert ("cmap", "magma") in events
    assert polygon_calls == [
        (
            (["cell-a", "end"],),
            {
                "closed": True,
                "facecolor": ("color", ("normalized", 3)),
                "edgecolor": "#333333",
                "linewidth": 0.25,
                "alpha": 0.95,
            },
        ),
        (
            (["cell-b", "end"],),
            {
                "closed": True,
                "facecolor": ("color", ("normalized", 1)),
                "edgecolor": "#333333",
                "linewidth": 0.25,
                "alpha": 0.95,
            },
        ),
    ]
    assert figure_calls == [
        (
            "colorbar",
            (events[[event[0] for event in events].index("scalar")][1],),
            {"ax": axis, "label": "Polygons per H3 cell (log scale)"},
        ),
        ("text", (0.5, 0.01, caption), {"ha": "center", "fontsize": 8}),
    ]
    assert ("array", []) in events
    assert events[-2] == ("save", figure, tmp_path / "map.png")
    assert events[-1] == ("close", figure)


def test_renderer_empty_branch_uses_exact_explanatory_text_and_caption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    axis_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    figure_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    events: list[object] = []

    class Axis:
        def __init__(self) -> None:
            self.transAxes = object()

        def _record(self, name: str, *args: object, **kwargs: object) -> None:
            axis_calls.append((name, args, kwargs))

        def set_facecolor(self, value: object) -> None:
            self._record("set_facecolor", value)

        def set_xlim(self, *values: object) -> None:
            self._record("set_xlim", *values)

        def set_ylim(self, *values: object) -> None:
            self._record("set_ylim", *values)

        def set_xticks(self, values: Iterable[object]) -> None:
            self._record("set_xticks", tuple(values))

        def set_yticks(self, values: Iterable[object]) -> None:
            self._record("set_yticks", tuple(values))

        def set_xlabel(self, value: object) -> None:
            self._record("set_xlabel", value)

        def set_ylabel(self, value: object) -> None:
            self._record("set_ylabel", value)

        def set_title(self, value: object) -> None:
            self._record("set_title", value)

        def grid(self, *args: object, **kwargs: object) -> None:
            self._record("grid", *args, **kwargs)

        def set_aspect(self, *args: object, **kwargs: object) -> None:
            self._record("set_aspect", *args, **kwargs)

        def text(self, *args: object, **kwargs: object) -> None:
            self._record("text", *args, **kwargs)

    class Figure:
        def text(self, *args: object, **kwargs: object) -> None:
            figure_calls.append(("text", args, kwargs))

    axis = Axis()
    figure = Figure()
    monkeypatch.setattr(rendering.plt, "subplots", lambda **_kwargs: (figure, axis))
    monkeypatch.setattr(rendering, "draw_landmasses", lambda *_args: events.append("land"))
    monkeypatch.setattr(rendering, "atomic_save_png", lambda *_args: events.append("save"))
    monkeypatch.setattr(rendering.plt, "close", lambda fig: events.append(("close", fig)))

    caption = rendering.render_polygon_density(
        PolygonDensitySummary(5, 0, 0, ()),
        tmp_path / "empty.png",
    )

    assert axis_calls[-1] == (
        "text",
        (0.5, 0.5, "No regional polygon rows/centroids"),
        {"transform": axis.transAxes, "ha": "center"},
    )
    assert figure_calls == [("text", (0.5, 0.01, caption), {"ha": "center", "fontsize": 8})]
    assert caption == (
        "H3 resolution 5; 0 occupied cells across 0 regional rows/centroids "
        "(regional polygon rows/centroids); "
        "logarithmic scale. Natural Earth 1:110m land backdrop."
    )
    assert events == ["land", "save", ("close", figure)]


def test_renderer_empty_global_branch_uses_exact_explanatory_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    text_calls: list[tuple[object, ...]] = []

    class Axis:
        def __init__(self) -> None:
            self.transAxes = object()

        def set_facecolor(self, _value: object) -> None:
            pass

        def set_xlim(self, *_values: object) -> None:
            pass

        def set_ylim(self, *_values: object) -> None:
            pass

        def set_xticks(self, _values: object) -> None:
            pass

        def set_yticks(self, _values: object) -> None:
            pass

        def set_xlabel(self, _value: object) -> None:
            pass

        def set_ylabel(self, _value: object) -> None:
            pass

        def set_title(self, _value: object) -> None:
            pass

        def grid(self, *_args: object, **_kwargs: object) -> None:
            pass

        def set_aspect(self, *_args: object, **_kwargs: object) -> None:
            pass

        def text(self, *args: object, **_kwargs: object) -> None:
            text_calls.append(args)

    class Figure:
        def text(self, *_args: object, **_kwargs: object) -> None:
            pass

    axis = Axis()
    monkeypatch.setattr(rendering.plt, "subplots", lambda **_kwargs: (Figure(), axis))
    monkeypatch.setattr(rendering, "draw_landmasses", lambda *_args: None)
    monkeypatch.setattr(rendering, "atomic_save_png", lambda *_args: None)
    monkeypatch.setattr(rendering.plt, "close", lambda _figure: None)

    rendering.render_polygon_density(
        PolygonDensitySummary(5, 0, 0, (), extracted_text_only=True),
        tmp_path / "empty-global.png",
    )

    assert text_calls == [
        (
            0.5,
            0.5,
            "No unique polygons with extracted text; regional overlap duplicates removed globally",
        )
    ]


def test_renderer_uses_one_as_the_singleton_log_scale_upper_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    norm_calls: list[dict[str, object]] = []
    real_log_norm = rendering.colors.LogNorm

    def log_norm(
        *,
        vmin: float | None = None,
        vmax: float | None = None,
        clip: bool = False,
    ) -> object:
        norm_calls.append({"vmin": vmin, "vmax": vmax})
        return real_log_norm(vmin=vmin, vmax=vmax, clip=clip)

    monkeypatch.setattr(rendering.colors, "LogNorm", log_norm)
    monkeypatch.setattr(rendering, "draw_landmasses", lambda *_args: None)
    monkeypatch.setattr(
        rendering,
        "cell_boundary_rings",
        lambda _cell: [[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, -1.0)]],
    )
    monkeypatch.setattr(rendering, "atomic_save_png", lambda *_args: None)

    rendering.render_polygon_density(
        PolygonDensitySummary(5, 1, 1, (("cell", 1),)),
        tmp_path / "singleton.png",
    )

    assert norm_calls == [{"vmin": 0.5, "vmax": 1.0}]


def test_renderer_uses_exact_world_tick_range_bounds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import rendering

    range_calls: list[tuple[int, ...]] = []
    real_range = builtins.range

    def range_spy(*args: int) -> range:
        range_calls.append(args)
        return real_range(*args)

    monkeypatch.setattr(rendering, "range", range_spy, raising=False)
    monkeypatch.setattr(rendering, "draw_landmasses", lambda *_args: None)
    monkeypatch.setattr(rendering, "atomic_save_png", lambda *_args: None)

    rendering.render_polygon_density(
        PolygonDensitySummary(5, 0, 0, ()),
        tmp_path / "ticks.png",
    )

    assert range_calls == [(-180, 181, 30), (-90, 91, 30)]


def test_build_polygon_density_map_forwards_inputs_and_returns_render_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}
    summary = PolygonDensitySummary(
        h3_resolution=7,
        polygon_row_count=11,
        occupied_cell_count=13,
        cells=(),
        aggregation_mode="global_unique_text",
    )

    def compute_summary(root: Path, **kwargs: object) -> PolygonDensitySummary:
        calls["summary"] = (root, kwargs)
        return summary

    def render_polygon_density(received: PolygonDensitySummary, destination: Path) -> str:
        calls["render"] = (received, destination)
        return "caption"

    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.polygon_density.compute_polygon_density_summary",
        compute_summary,
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.polygon_density.render_polygon_density",
        render_polygon_density,
    )

    result = build_polygon_density_map(
        tmp_path,
        h3_resolution=9,
        source_names={"monaco-latest.osm.pbf"},
        extracted_text_only=True,
        aggregation_mode="global_unique_text",
    )

    assert calls["summary"] == (
        tmp_path,
        {
            "h3_resolution": 9,
            "source_names": {"monaco-latest.osm.pbf"},
            "extracted_text_only": True,
            "aggregation_mode": "global_unique_text",
        },
    )
    assert calls["render"] == (summary, tmp_path / POLYGON_DENSITY_ASSET_REL_PATH)
    assert result.output_path == tmp_path / POLYGON_DENSITY_ASSET_REL_PATH
    assert result.h3_resolution == 7
    assert result.polygon_row_count == 11
    assert result.occupied_cell_count == 13
    assert result.caption == "caption"
    assert result.aggregation_mode == "global_unique_text"


def test_build_polygon_density_map_defaults_missing_aggregation_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from osm_polygon_website_tag.reporting.geographic import polygon_density

    summary = SimpleNamespace(
        h3_resolution=7,
        polygon_row_count=11,
        occupied_cell_count=13,
        aggregation_mode=None,
    )
    monkeypatch.setattr(
        polygon_density,
        "compute_polygon_density_summary",
        lambda *_args, **_kwargs: summary,
    )
    monkeypatch.setattr(
        polygon_density,
        "render_polygon_density",
        lambda *_args, **_kwargs: "caption",
    )

    result = build_polygon_density_map(tmp_path)

    assert result.aggregation_mode == "regional_rows"


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
