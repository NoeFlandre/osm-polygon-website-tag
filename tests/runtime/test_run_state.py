"""Tests for the run-state directory layout and source tracking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm_polygon_website_tag.runtime.run_state import (
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_INITIALIZED,
    SourceFingerprint,
    expected_source_inventory,
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
    source_inventory_matches,
    transition_status,
    upsert_run_metadata,
)


def _write_pbf_with_size(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_run_id_format(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="test-run-id-format")
    assert run_dir.name == "test-run-id-format"


def test_initialise_run_creates_layout(tmp_path: Path) -> None:
    run_id = "20240101T000000Z-test"
    run_dir, _state = initialise_run(tmp_path, run_id=run_id)
    assert run_dir == tmp_path / run_id
    assert (run_dir / "polygons").is_dir()
    assert (run_dir / "analysis_observations").is_dir()
    assert (run_dir / "rejections").is_dir()
    assert (run_dir / "analysis").is_dir()
    assert (run_dir / "manifests").is_dir()
    assert (run_dir / "manifests" / "run.json").is_file()
    assert (run_dir / "manifests" / "sources.json").is_file()


def test_initialise_run_writes_expected_sources_when_provided(tmp_path: Path) -> None:
    fp_a = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=10, mtime_ns=12345)
    fp_b = SourceFingerprint(filename="b-latest.osm.pbf", size_bytes=20, mtime_ns=67890)
    _run_dir, _state = initialise_run(tmp_path, run_id="r", expected_sources=[fp_a, fp_b])
    inv = expected_source_inventory(tmp_path / "r")
    assert inv == [
        {"filename": "a-latest.osm.pbf", "size_bytes": 10, "mtime_ns": 12345},
        {"filename": "b-latest.osm.pbf", "size_bytes": 20, "mtime_ns": 67890},
    ]


def test_initialise_run_omits_source_sha256(tmp_path: Path) -> None:
    fp_a = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=10, mtime_ns=12345)
    _run_dir, _state = initialise_run(tmp_path, run_id="r", expected_sources=[fp_a])
    inv = expected_source_inventory(tmp_path / "r")
    for entry in inv:
        assert "sha256" not in entry


def test_load_run_round_trips_metadata(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    upsert_run_metadata(state, {"python": "3.12"})
    reloaded = load_run(run_dir)
    assert reloaded.metadata["python"] == "3.12"


def test_source_fingerprint_captures_size_and_mtime(tmp_path: Path) -> None:
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    assert fp.filename == "monaco-latest.osm.pbf"
    assert fp.size_bytes == 4
    assert isinstance(fp.mtime_ns, int)


def test_source_fingerprint_has_no_sha256_field() -> None:
    fp = SourceFingerprint(filename="x", size_bytes=1, mtime_ns=2)
    assert not hasattr(fp, "sha256")


def test_source_fingerprint_changes_with_size(tmp_path: Path) -> None:
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"a")
    fp1 = snapshot_source_fingerprint(p)
    p.write_bytes(b"ab")
    fp2 = snapshot_source_fingerprint(p)
    assert fp1.size_bytes != fp2.size_bytes


def test_record_processed_source_appends(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=10, observation_row_count=10)
    sources_path = run_dir / "manifests" / "sources.json"
    data = json.loads(sources_path.read_text())
    assert len(data) == 1
    assert data[0]["filename"] == "monaco-latest.osm.pbf"
    assert data[0]["public_row_count"] == 10
    assert data[0]["observation_row_count"] == 10


def test_record_processed_source_dedupes_by_filename(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=10, observation_row_count=10)
    record_processed_source(state, fp, public_row_count=11, observation_row_count=11)
    sources_path = run_dir / "manifests" / "sources.json"
    data = json.loads(sources_path.read_text())
    assert len(data) == 1
    assert data[0]["public_row_count"] == 11


def test_run_metadata_persists_start_and_end(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    upsert_run_metadata(state, {"started_at": "2024-01-01T00:00:00Z"})
    upsert_run_metadata(state, {"ended_at": "2024-01-01T00:01:00Z"})
    reloaded = load_run(run_dir)
    assert reloaded.metadata["started_at"] == "2024-01-01T00:00:00Z"
    assert reloaded.metadata["ended_at"] == "2024-01-01T00:01:00Z"


def test_upsert_run_metadata_rejects_status_change(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]  # noqa: RUF059
    with pytest.raises(ValueError, match="transition_status"):
        upsert_run_metadata(state, {"status": STATUS_COMPLETE})


def test_run_state_layout_does_not_create_unrequested_dirs(tmp_path: Path) -> None:
    parent_files = set(tmp_path.iterdir())
    initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    new_entries = set(tmp_path.iterdir()) - parent_files
    assert len(new_entries) == 1
    assert new_entries.pop().name == "abc"


def test_run_metadata_keys_are_sorted(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    upsert_run_metadata(state, {"z": 1, "a": 2, "m": 3})
    run_json = (run_dir / "manifests" / "run.json").read_text()
    assert run_json.index('"a"') < run_json.index('"m"') < run_json.index('"z"')


def test_run_state_detects_source_mutation_via_size(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=10, observation_row_count=10)
    p.write_bytes(b"different")
    new_fp = snapshot_source_fingerprint(p)
    assert new_fp != fp
    assert source_inventory_matches_via_size_mtime(_run_dir, new_fp) is False


def source_inventory_matches_via_size_mtime(run_dir: Path, fp: SourceFingerprint) -> bool:
    """Wrap to keep the production symbol isolated from the test helper."""
    return __import__(
        "osm_polygon_website_tag.runtime.run_state", fromlist=["source_is_unchanged"]
    ).source_is_unchanged(load_run(run_dir), fp)


def test_source_fingerprint_is_hashable(tmp_path: Path) -> None:
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    s = {fp}
    assert fp in s


def test_load_run_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_run(tmp_path / "missing")


def test_run_state_initial_status_is_initialized(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    assert state.metadata["status"] == STATUS_INITIALIZED


def test_transition_status_advances_through_pipeline(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    from osm_polygon_website_tag.runtime.run_state import (
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_ENRICHED,
        STATUS_ENRICHING,
        STATUS_EXTRACTED,
        STATUS_EXTRACTING,
        STATUS_VERIFIED,
    )

    transition_status(state, STATUS_EXTRACTING)
    transition_status(state, STATUS_EXTRACTED)
    transition_status(state, STATUS_ENRICHING)
    transition_status(state, STATUS_ENRICHED)
    transition_status(state, STATUS_ANALYZED)
    transition_status(state, STATUS_CARD_BUILT)
    transition_status(state, STATUS_VERIFIED)
    transition_status(state, STATUS_COMPLETE)
    reloaded = load_run(_run_dir)
    assert reloaded.metadata["status"] == STATUS_COMPLETE


def test_complete_run_can_reopen_only_for_schema_enrichment(tmp_path: Path) -> None:
    from osm_polygon_website_tag.runtime.run_state import (
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_ENRICHED,
        STATUS_ENRICHING,
        STATUS_EXTRACTED,
        STATUS_EXTRACTING,
        STATUS_VERIFIED,
    )

    _run_dir, state = initialise_run(tmp_path, run_id="migration")
    for status in (
        STATUS_EXTRACTING,
        STATUS_EXTRACTED,
        STATUS_ENRICHING,
        STATUS_ENRICHED,
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_VERIFIED,
        STATUS_COMPLETE,
    ):
        transition_status(state, status)

    transition_status(state, STATUS_ENRICHING)

    assert state.metadata["status"] == STATUS_ENRICHING
    with pytest.raises(ValueError):
        transition_status(state, STATUS_EXTRACTING)


def test_transition_status_rejects_illegal_step(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    from osm_polygon_website_tag.runtime.run_state import STATUS_COMPLETE

    with pytest.raises(ValueError, match="illegal"):
        transition_status(state, STATUS_COMPLETE)


def test_transition_to_incomplete_from_any_state(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    from osm_polygon_website_tag.runtime.run_state import STATUS_EXTRACTING

    transition_status(state, STATUS_EXTRACTING)
    transition_status(state, STATUS_INCOMPLETE)
    assert state.metadata["status"] == STATUS_INCOMPLETE


def test_complete_state_rejects_generic_incomplete_transition(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")  # type: ignore[arg-type]
    from osm_polygon_website_tag.runtime.run_state import (
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
        STATUS_ENRICHED,
        STATUS_ENRICHING,
        STATUS_EXTRACTED,
        STATUS_EXTRACTING,
        STATUS_VERIFIED,
    )

    transition_status(state, STATUS_EXTRACTING)
    transition_status(state, STATUS_EXTRACTED)
    transition_status(state, STATUS_ENRICHING)
    transition_status(state, STATUS_ENRICHED)
    transition_status(state, STATUS_ANALYZED)
    transition_status(state, STATUS_CARD_BUILT)
    transition_status(state, STATUS_VERIFIED)
    transition_status(state, STATUS_COMPLETE)
    with pytest.raises(ValueError, match="illegal"):
        transition_status(state, STATUS_INCOMPLETE)


def test_source_inventory_matches_happy_path(tmp_path: Path) -> None:
    fp_a = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=10, mtime_ns=12345)
    fp_b = SourceFingerprint(filename="b-latest.osm.pbf", size_bytes=20, mtime_ns=67890)
    _run_dir, state = initialise_run(tmp_path, run_id="r", expected_sources=[fp_a, fp_b])
    record_processed_source(state, fp_a, public_row_count=1, observation_row_count=1)
    record_processed_source(state, fp_b, public_row_count=1, observation_row_count=1)
    assert source_inventory_matches(_run_dir) is True


def test_source_inventory_matches_detects_mutation(tmp_path: Path) -> None:
    fp_a = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=10, mtime_ns=12345)
    _run_dir, state = initialise_run(tmp_path, run_id="r", expected_sources=[fp_a])
    mutated = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=11, mtime_ns=12345)
    record_processed_source(state, mutated, public_row_count=1, observation_row_count=1)
    assert source_inventory_matches(_run_dir) is False
