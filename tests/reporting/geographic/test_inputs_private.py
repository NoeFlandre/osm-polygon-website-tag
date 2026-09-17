"""Direct contracts for bounded geographic input helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.arrow import call_arrow_kernel
from osm_polygon_website_tag.reporting.geographic.inputs import (
    _coordinate_value,
    _iter_batch_rows,
    _iter_path_rows,
    _iter_unique_text_batch_rows,
    _iter_unique_text_path_rows,
    _path_columns,
    _reserve_text_identity,
    _row_is_eligible,
    _select_polygon_parquets,
    _successful_non_empty_text,
    _text_path_columns,
    _text_polygon_parquets,
    _text_success_mask,
    _validated_coordinates,
    iter_lat_lon_runs,
    iter_unique_text_lat_lon_runs,
)


def test_input_helpers_select_columns_and_validate_coordinates() -> None:
    names = {
        "lat",
        "lon",
        "osm_type",
        "osm_id",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    }
    assert _path_columns(Path("a.parquet"), names, extracted_text_only=False) == ["lat", "lon"]
    assert _path_columns(Path("a.parquet"), names, extracted_text_only=True) == [
        "lat",
        "lon",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    ]
    assert _row_is_eligible(None, 0)
    assert _row_is_eligible([False, True], 1)
    assert not _row_is_eligible([False, True], 0)
    assert _coordinate_value(Path("a.parquet"), 2, 1, 2, lat_is_null=False, lon_is_null=False) == (
        Path("a.parquet"),
        2,
        1.0,
        2.0,
    )
    with pytest.raises(ValueError, match="null coordinate"):
        _coordinate_value(Path("a.parquet"), 2, None, 2, lat_is_null=True, lon_is_null=False)


def test_input_helpers_build_null_safe_text_mask_and_coordinate_iterator() -> None:
    batch = pa.record_batch(
        [
            pa.array(["text", None, ""]),
            pa.array(["success", None, "absent"]),
            pa.array([None, "contact", ""]),
            pa.array([None, "success", "absent"]),
        ],
        names=[
            "website_text",
            "website_text_status",
            "contact_website_text",
            "contact_website_text_status",
        ],
    )
    assert _text_success_mask(batch).tolist() == [True, True, False]
    values = zip(
        [1.0, 2.0, 3.0],
        [4.0, 5.0, 6.0],
        [False, False, False],
        [False, False, False],
        strict=True,
    )
    assert list(_validated_coordinates(Path("a.parquet"), values, [True, False, True], 10)) == [
        (Path("a.parquet"), 10, 1.0, 4.0),
        (Path("a.parquet"), 12, 3.0, 6.0),
    ]
    assert call_arrow_kernel("equal", pa.array(["success"]), "success").to_pylist() == [True]


def test_select_polygon_parquets_supports_unfiltered_and_source_scoped_reads(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "polygons"
    directory.mkdir()
    france = directory / "france-latest.parquet"
    monaco = directory / "monaco-latest.parquet"
    france.touch()
    monaco.touch()

    assert _select_polygon_parquets(directory, None) == [france, monaco]
    assert _select_polygon_parquets(directory, {"monaco-latest.osm.pbf"}) == [monaco]
    assert _select_polygon_parquets(directory, {"missing-latest.osm.pbf"}) == []


def test_text_column_contract_requires_identity_only_for_global_deduplication() -> None:
    text_names = {
        "lat",
        "lon",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    }
    assert _text_path_columns(text_names, include_identity=False) == [
        "lat",
        "lon",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    ]
    assert _text_path_columns(text_names, include_identity=True) is None
    assert _text_path_columns(text_names | {"osm_type", "osm_id"}, include_identity=True) == [
        "osm_type",
        "osm_id",
        "lat",
        "lon",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    ]


def test_path_columns_reject_missing_coordinates_and_skip_unproven_text() -> None:
    with pytest.raises(ValueError, match="missing coordinate columns"):
        _path_columns(Path("missing.parquet"), {"lat"}, extracted_text_only=False)
    assert (
        _path_columns(
            Path("legacy.parquet"),
            {"lat", "lon", "website_text"},
            extracted_text_only=True,
        )
        is None
    )


def test_unique_text_batch_rows_reserve_qualified_identities_once() -> None:
    batch = pa.record_batch(
        [
            pa.array(["way", "way", "relation", "way", None]),
            pa.array(["1", "1", "3", "4", "5"]),
            pa.array([10.0, 11.0, 12.0, 13.0, 14.0]),
            pa.array([20.0, 21.0, 22.0, 23.0, 24.0]),
            pa.array([" text ", "", None, "failed", "missing type"]),
            pa.array(["success", "success", "absent", "failed", "success"]),
            pa.array([None, "contact", "contact", None, None]),
            pa.array(["absent", "success", "success", "absent", "absent"]),
        ],
        names=[
            "osm_type",
            "osm_id",
            "lat",
            "lon",
            "website_text",
            "website_text_status",
            "contact_website_text",
            "contact_website_text_status",
        ],
    )
    seen: set[tuple[str, int]] = set()

    assert list(_iter_unique_text_batch_rows(Path("source.parquet"), batch, 7, seen)) == [
        (Path("source.parquet"), 7, 10.0, 20.0),
        (Path("source.parquet"), 9, 12.0, 22.0),
    ]
    assert seen == {("way", 1), ("relation", 3)}


def test_unique_text_batch_rows_rejects_null_coordinates_after_text_selection() -> None:
    batch = pa.record_batch(
        [
            pa.array(["way"]),
            pa.array([1]),
            pa.array([None], type=pa.float64()),
            pa.array([20.0]),
            pa.array(["text"]),
            pa.array(["success"]),
            pa.array([None], type=pa.string()),
            pa.array(["absent"]),
        ],
        names=[
            "osm_type",
            "osm_id",
            "lat",
            "lon",
            "website_text",
            "website_text_status",
            "contact_website_text",
            "contact_website_text_status",
        ],
    )
    with pytest.raises(ValueError, match=r"null coordinate.*row 0"):
        list(_iter_unique_text_batch_rows(Path("source.parquet"), batch, 0, set()))


@pytest.mark.parametrize(
    ("text_values", "osm_type", "osm_id", "expected"),
    [
        (None, "way", 1, True),
        ([False], "way", 1, False),
        ([True], None, 1, False),
        ([True], "way", None, False),
        ([True], "way", "12", True),
    ],
)
def test_reserve_text_identity_is_fail_closed_and_normalizes_ids(
    text_values: list[bool] | None,
    osm_type: str | None,
    osm_id: int | str | None,
    expected: bool,
) -> None:
    seen: set[tuple[str, int]] = set()
    assert _reserve_text_identity(text_values, 0, osm_type, osm_id, seen) is expected
    if expected:
        assert osm_type is not None
        assert osm_id is not None
        assert seen == {(str(osm_type), int(osm_id))}

    if expected:
        assert not _reserve_text_identity(text_values, 0, osm_type, osm_id, seen)


def test_batch_and_path_iterators_preserve_text_filter_and_row_offsets() -> None:
    batch = pa.record_batch(
        [
            pa.array([1.0, 2.0, 3.0]),
            pa.array([4.0, 5.0, 6.0]),
            pa.array(["text", "", "contact"]),
            pa.array(["success", "success", "absent"]),
            pa.array([None, None, "contact"]),
            pa.array(["absent", "absent", "success"]),
        ],
        names=[
            "lat",
            "lon",
            "website_text",
            "website_text_status",
            "contact_website_text",
            "contact_website_text_status",
        ],
    )
    assert list(_iter_batch_rows(Path("source.parquet"), batch, 10, extracted_text_only=False)) == [
        (Path("source.parquet"), 10, 1.0, 4.0),
        (Path("source.parquet"), 11, 2.0, 5.0),
        (Path("source.parquet"), 12, 3.0, 6.0),
    ]
    assert list(_iter_batch_rows(Path("source.parquet"), batch, 10, extracted_text_only=True)) == [
        (Path("source.parquet"), 10, 1.0, 4.0),
        (Path("source.parquet"), 12, 3.0, 6.0),
    ]


def test_path_iterators_select_bounded_columns_and_accumulate_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path("source.parquet")
    first = pa.record_batch([pa.array([1.0, 2.0]), pa.array([3.0, 4.0])], names=["lat", "lon"])
    second = pa.record_batch([pa.array([5.0]), pa.array([6.0])], names=["lat", "lon"])
    calls: list[dict[str, object]] = []
    column_calls: list[tuple[Path, bool, bool]] = []

    class FakeParquet:
        schema_arrow = SimpleNamespace(names=["lat", "lon"])

        def iter_batches(self, **kwargs: object):
            calls.append(kwargs)
            yield first
            yield second

    monkeypatch.setattr(pq, "ParquetFile", lambda _path: FakeParquet())
    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.inputs._path_columns",
        lambda path, names, *, extracted_text_only, include_identity=False: (
            column_calls.append((path, extracted_text_only, include_identity)) or ["lat", "lon"]
        ),
    )

    assert list(_iter_path_rows(path, extracted_text_only=False)) == [
        (path, 0, 1.0, 3.0),
        (path, 1, 2.0, 4.0),
        (path, 2, 5.0, 6.0),
    ]
    assert calls == [{"columns": ["lat", "lon"], "batch_size": 8192}]
    assert column_calls == [(path, False, False)]


def test_unique_text_path_iterator_forwards_identity_columns_and_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path("source.parquet")
    batch = SimpleNamespace(num_rows=0)
    calls: list[dict[str, object]] = []
    forwarded: list[tuple[Path, object, int, set[tuple[str, int]]]] = []
    seen: set[tuple[str, int]] = set()

    class FakeParquet:
        schema_arrow = SimpleNamespace(names=["all"])

        def iter_batches(self, **kwargs: object):
            calls.append(kwargs)
            yield batch

    monkeypatch.setattr(pq, "ParquetFile", lambda _path: FakeParquet())
    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.inputs._path_columns",
        lambda path, names, *, extracted_text_only, include_identity=False: (
            ["osm_type", "osm_id", "lat", "lon"]
            if extracted_text_only and include_identity
            else None
        ),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.inputs._iter_unique_text_batch_rows",
        lambda path, received, offset, received_seen: (
            forwarded.append((path, received, offset, received_seen)) or iter(())
        ),
    )

    assert list(_iter_unique_text_path_rows(path, seen)) == []
    assert calls == [{"columns": ["osm_type", "osm_id", "lat", "lon"], "batch_size": 8192}]
    assert forwarded == [(path, batch, 0, seen)]


def test_public_iterator_wrappers_forward_scope_and_text_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path("source.parquet")
    path_calls: list[tuple[Path, bool]] = []
    unique_calls: list[tuple[Path, set[tuple[str, int]]]] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.inputs.sorted_public_polygon_parquets",
        lambda run_dir, *, source_names: (
            [path] if (run_dir, source_names) == ("run", {"source.osm.pbf"}) else []
        ),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.inputs._iter_path_rows",
        lambda received_path, *, extracted_text_only: (
            path_calls.append((received_path, extracted_text_only)) or iter(())
        ),
    )
    assert (
        list(iter_lat_lon_runs("run", source_names={"source.osm.pbf"}, extracted_text_only=True))
        == []
    )
    assert path_calls == [(path, True)]

    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.inputs._text_polygon_parquets",
        lambda run_dir, *, source_names: (
            [path] if (run_dir, source_names) == ("run", {"source.osm.pbf"}) else []
        ),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.reporting.geographic.inputs._iter_unique_text_path_rows",
        lambda received_path, received_seen: (
            unique_calls.append((received_path, received_seen)) or iter(())
        ),
    )
    assert list(iter_unique_text_lat_lon_runs("run", source_names={"source.osm.pbf"})) == []
    assert unique_calls == [(path, set())]


def test_text_polygon_iterator_prefers_regional_copies_and_falls_back_to_public(
    tmp_path: Path,
) -> None:
    regional_root = tmp_path / "regional"
    regional_observations = regional_root / "analysis_observations"
    regional_polygons = regional_root / "polygons"
    regional_polygons.mkdir(parents=True)
    regional_path = regional_polygons / "source.parquet"
    regional_path.touch()
    (tmp_path / "polygons").mkdir()
    public_path = tmp_path / "polygons" / "source.parquet"
    public_path.touch()
    (tmp_path / "analysis_observations").symlink_to(regional_observations, target_is_directory=True)

    assert _text_polygon_parquets(tmp_path, source_names={"source.osm.pbf"}) == [regional_path]

    (tmp_path / "analysis_observations").unlink()
    assert _text_polygon_parquets(tmp_path, source_names={"source.osm.pbf"}) == [public_path]


def test_successful_non_empty_text_trims_whitespace_and_requires_success() -> None:
    mask = _successful_non_empty_text(
        pa.array([" text ", "   ", "text", None]),
        pa.array(["success", "success", "failed", "success"]),
    )
    assert mask.to_pylist() == [True, False, False, None]
