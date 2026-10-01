"""Row-level invariant checks over polygon, observation and rejection shards."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.reporting.verification.rows import verify_row_invariants


def _write(root: Path, directory: str, rows: list[dict[str, object]]) -> None:
    (root / directory).mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), root / directory / "shard.parquet")


def _polygon(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "has_any_website": True,
        "has_website": True,
        "has_contact_website": False,
        "website": "https://example.org",
        "contact_website": None,
        "osm_type": "way",
        "lat": 1.0,
        "lon": 2.0,
        "area_m2": 3.0,
    }
    return {**row, **overrides}


def _observation(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "has_any_website": True,
        "has_website": True,
        "has_contact_website": False,
        "website": "https://example.org",
        "contact_website": None,
        "has_wikidata": False,
        "wikidata": None,
        "osm_type": "relation",
    }
    return {**row, **overrides}


def _rejection(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "has_any_website": True,
        "has_website": True,
        "has_contact_website": False,
        "website": "https://example.org",
        "contact_website": None,
        "osm_type": "way",
        "rejection_kind": "geometry_error",
    }
    return {**row, **overrides}


def _errors(root: Path) -> list[str]:
    errors: list[str] = []
    verify_row_invariants(root, errors)
    return errors


def test_an_empty_run_has_no_row_violations(tmp_path: Path) -> None:
    assert _errors(tmp_path) == []


def test_valid_rows_in_every_shard_kind_pass(tmp_path: Path) -> None:
    _write(tmp_path, "polygons", [_polygon()])
    _write(tmp_path, "analysis_observations", [_observation()])
    _write(tmp_path, "rejections", [_rejection()])

    assert _errors(tmp_path) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"has_any_website": False, "has_website": False},
        {"website": "  "},
        {"osm_type": "node"},
        {"lat": float("nan")},
        {"area_m2": -1.0},
    ],
)
def test_a_bad_public_row_is_counted_under_its_label(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    _write(tmp_path, "polygons", [_polygon(), _polygon(**bad)])

    assert _errors(tmp_path) == ["public row invariant violations: 1"]


def test_a_bad_observation_is_counted_under_its_label(tmp_path: Path) -> None:
    _write(tmp_path, "analysis_observations", [_observation(has_wikidata=True)] * 2)

    assert _errors(tmp_path) == ["comparison row invariant violations: 2"]


def test_a_rejection_without_a_kind_is_counted_under_its_label(tmp_path: Path) -> None:
    _write(tmp_path, "rejections", [_rejection(rejection_kind="")])

    assert _errors(tmp_path) == ["rejection row invariant violations: 1"]


def test_violations_in_several_kinds_are_all_reported_in_order(tmp_path: Path) -> None:
    _write(tmp_path, "polygons", [_polygon(osm_type="node")])
    _write(tmp_path, "rejections", [_rejection(osm_type="node")])

    assert _errors(tmp_path) == [
        "public row invariant violations: 1",
        "rejection row invariant violations: 1",
    ]


def test_an_unreadable_shard_is_reported_as_a_finding_not_raised(tmp_path: Path) -> None:
    _write(tmp_path, "polygons", [{"unrelated": 1}])

    errors = _errors(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith("row invariant verification failed: ")


def test_verification_keeps_its_database_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _write(run, "polygons", [_polygon()])

    assert _errors(run) == []
    assert list(workdir.iterdir()) == []
