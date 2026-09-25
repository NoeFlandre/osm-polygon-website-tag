"""Behavior of bounded geographic coordinate input."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.arrow import call_arrow_kernel
from osm_polygon_website_tag.reporting.geographic.inputs import (
    _coordinate_value,
    _iter_batch_rows,
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
    assert _path_columns(
        Path("identity.parquet"),
        {
            "lat",
            "lon",
            "osm_type",
            "osm_id",
            "website_text",
            "website_text_status",
            "contact_website_text",
            "contact_website_text_status",
        },
        extracted_text_only=True,
        include_identity=True,
    ) == [
        "osm_type",
        "osm_id",
        "lat",
        "lon",
        "website_text",
        "website_text_status",
        "contact_website_text",
        "contact_website_text_status",
    ]


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


def test_unique_text_batch_rows_rejects_null_longitude_after_text_selection() -> None:
    batch = pa.record_batch(
        [
            pa.array(["way"]),
            pa.array([1]),
            pa.array([10.0]),
            pa.array([None], type=pa.float64()),
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


def test_text_polygon_iterator_prefers_regional_copies_and_falls_back_to_public(
    tmp_path: Path,
) -> None:
    regional_root = tmp_path / "regional"
    regional_observations = regional_root / "analysis_observations"
    regional_polygons = regional_root / "polygons"
    regional_polygons.mkdir(parents=True)
    regional_path = regional_polygons / "source.parquet"
    regional_path.touch()
    (regional_polygons / "other.parquet").touch()
    (tmp_path / "polygons").mkdir()
    public_path = tmp_path / "polygons" / "source.parquet"
    public_path.touch()
    (tmp_path / "polygons" / "other.parquet").touch()
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


def test_validated_coordinates_rejects_null_longitude() -> None:
    values = [(1.0, 2.0, False, True)]
    with pytest.raises(ValueError, match=r"null coordinate.*row 0"):
        list(_validated_coordinates(Path("source.parquet"), values, None, 0))


def test_text_polygon_parquets_falls_back_when_regional_resolution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    observations_dir = root / "analysis_observations"
    regional_target = root / "regional" / "analysis_observations"
    regional_target.parent.mkdir()
    observations_dir.symlink_to(regional_target, target_is_directory=True)
    public = root / "polygons" / "source.parquet"
    public.parent.mkdir()
    public.touch()
    original_resolve = Path.resolve

    def resolve(path: Path) -> Path:
        if path == observations_dir:
            raise OSError("unresolvable regional observations")
        return original_resolve(path)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert _text_polygon_parquets(root, source_names={"source.osm.pbf"}) == [public]


def _write_text_shard(path: Path, rows: list[dict[str, object]]) -> Path:
    schema = pa.schema(
        [
            pa.field("osm_type", pa.string()),
            pa.field("osm_id", pa.int64()),
            pa.field("lat", pa.float64()),
            pa.field("lon", pa.float64()),
            pa.field("website_text", pa.string()),
            pa.field("website_text_status", pa.string()),
            pa.field("contact_website_text", pa.string()),
            pa.field("contact_website_text_status", pa.string()),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return path


def _text_row(
    osm_id: int,
    *,
    lat: float | None = 1.0,
    lon: float | None = 2.0,
    text: str | None = "text",
    status: str | None = "success",
) -> dict[str, object]:
    return {
        "osm_type": "way",
        "osm_id": osm_id,
        "lat": lat,
        "lon": lon,
        "website_text": text,
        "website_text_status": status,
        "contact_website_text": None,
        "contact_website_text_status": "absent",
    }


def test_path_rows_keep_offsets_across_batches(tmp_path: Path) -> None:
    rows = [_text_row(index, text="" if index < 16384 else "text") for index in range(16387)]
    path = _write_text_shard(tmp_path / "polygons" / "source.parquet", rows)

    everything = list(iter_lat_lon_runs(tmp_path))
    assert [row_index for _, row_index, _, _ in everything] == list(range(16387))
    assert list(iter_lat_lon_runs(tmp_path, extracted_text_only=True)) == [
        (path, index, 1.0, 2.0) for index in (16384, 16385, 16386)
    ]


def test_rows_without_any_text_status_are_not_extracted_text(tmp_path: Path) -> None:
    path = _write_text_shard(
        tmp_path / "polygons" / "source.parquet",
        [_text_row(1, text=None, status=None), _text_row(2)],
    )

    assert list(iter_lat_lon_runs(tmp_path, extracted_text_only=True)) == [(path, 1, 1.0, 2.0)]


def test_path_rows_reject_null_coordinates(tmp_path: Path) -> None:
    _write_text_shard(
        tmp_path / "polygons" / "source.parquet", [_text_row(1), _text_row(2, lat=None)]
    )

    with pytest.raises(ValueError, match=r"null coordinate in source.parquet row 1"):
        list(iter_lat_lon_runs(tmp_path))


@pytest.mark.parametrize("extracted_text_only", [False, True])
def test_path_rows_reject_null_longitude(tmp_path: Path, extracted_text_only: bool) -> None:
    _write_text_shard(
        tmp_path / "polygons" / "source.parquet", [_text_row(1), _text_row(2, lon=None)]
    )

    with pytest.raises(ValueError, match=r"null coordinate in source.parquet row 1"):
        list(iter_lat_lon_runs(tmp_path, extracted_text_only=extracted_text_only))


def test_unique_text_path_rows_reject_null_longitude(tmp_path: Path) -> None:
    path = _write_text_shard(tmp_path / "source.parquet", [_text_row(1), _text_row(2, lon=None)])

    with pytest.raises(ValueError, match=r"null coordinate in source.parquet row 1"):
        list(_iter_unique_text_path_rows(path, set()))


def test_path_rows_reject_missing_coordinate_columns(tmp_path: Path) -> None:
    path = tmp_path / "polygons" / "source.parquet"
    path.parent.mkdir()
    pq.write_table(pa.table({"lat": [1.0]}), path)

    with pytest.raises(ValueError, match=rf"missing coordinate columns \['lon'\] in {path}$"):
        list(iter_lat_lon_runs(tmp_path))
    with pytest.raises(ValueError, match=rf"missing coordinate columns \['lon'\] in {path}$"):
        list(_iter_unique_text_path_rows(path, set()))


def test_unique_text_path_rows_reserve_each_identity_once_across_batches(
    tmp_path: Path,
) -> None:
    rows = [
        _text_row(index % 3, status="failed" if index < 16384 else "success")
        for index in range(16388)
    ]
    path = _write_text_shard(tmp_path / "source.parquet", rows)
    seen: set[tuple[str, int]] = {("way", 2)}

    assert list(_iter_unique_text_path_rows(path, seen)) == [
        (path, 16384, 1.0, 2.0),
        (path, 16386, 1.0, 2.0),
    ]
    assert seen == {("way", 0), ("way", 1), ("way", 2)}


def test_unique_text_path_rows_skip_shards_without_identity(tmp_path: Path) -> None:
    full = _write_text_shard(tmp_path / "full.parquet", [_text_row(1)])
    path = tmp_path / "source.parquet"
    pq.write_table(pq.read_table(full).drop_columns(["osm_type", "osm_id"]), path)

    assert list(_iter_unique_text_path_rows(path, set())) == []


def test_unique_text_runs_honour_the_source_scope(tmp_path: Path) -> None:
    kept = _write_text_shard(tmp_path / "polygons" / "kept.parquet", [_text_row(1)])
    _write_text_shard(tmp_path / "polygons" / "other.parquet", [_text_row(2)])

    runs = list(iter_unique_text_lat_lon_runs(tmp_path, source_names={"kept.osm.pbf"}))

    assert [(path, row_index) for path, row_index, _, _ in runs] == [(kept, 0)]
