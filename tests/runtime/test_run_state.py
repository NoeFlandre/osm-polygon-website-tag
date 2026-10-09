"""Tests for the run-state directory layout and source tracking."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    STATUS_INCOMPLETE,
    STATUS_INITIALIZED,
    STATUS_VERIFIED,
    SourceFingerprint,
    SourceManifestEntry,
    _normalise_status_field,
    _read_json_document,
    _source_fingerprint_payload,
    _source_identity_matches,
    _source_inventory_entries_match,
    _validate_source_filename,
    _validate_source_numeric_fields,
    _validated_source_entry,
    _validated_status_count,
    atomic_write_json,
    default_run_id,
    expected_source_inventory,
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
    source_inventory_matches,
    source_is_unchanged,
    transition_status,
    update_public_shard_metadata,
    update_source_enrichment_status,
    upsert_run_metadata,
)


def _write_pbf_with_size(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_run_id_format(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="test-run-id-format")
    assert run_dir.name == "test-run-id-format"


def test_run_state_private_source_validation_helpers() -> None:
    fp = SourceFingerprint(filename="a.osm.pbf", size_bytes=2, mtime_ns=3)
    assert _source_fingerprint_payload(fp) == {
        "filename": "a.osm.pbf",
        "size_bytes": 2,
        "mtime_ns": 3,
    }
    assert (
        _validated_source_entry(
            {"filename": "a.osm.pbf", "size_bytes": 2, "mtime_ns": 3},
            label="sources",
            index=0,
        )["filename"]
        == "a.osm.pbf"
    )
    _validate_source_filename("a.osm.pbf", label="sources", index=0)
    _validate_source_numeric_fields({"size_bytes": 2, "mtime_ns": 3}, label="sources", index=0)
    with pytest.raises(ValueError, match="filename"):
        _validate_source_filename("", label="sources", index=0)
    with pytest.raises(ValueError, match="size_bytes"):
        _validate_source_numeric_fields(
            {"size_bytes": True, "mtime_ns": 3}, label="sources", index=0
        )
    assert _source_identity_matches(
        {"size_bytes": 2, "mtime_ns": 3}, {"size_bytes": 2, "mtime_ns": 3}
    )
    assert not _source_identity_matches(
        {"size_bytes": 2, "mtime_ns": 4}, {"size_bytes": 2, "mtime_ns": 3}
    )
    assert _source_inventory_entries_match(
        [{"filename": "a", "size_bytes": 1, "mtime_ns": 2}],
        [
            cast(
                SourceManifestEntry,
                {"filename": "a", "size_bytes": 1, "mtime_ns": 2, "extra": 1},
            )
        ],
    )


def test_read_json_document_normalizes_corruption(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text('{"ok": true}', encoding="utf-8")
    assert _read_json_document(valid, label="document") == {"ok": True}
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^invalid document JSON: "):
        _read_json_document(invalid, label="document")


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
    inventory_path = tmp_path / "r" / "manifests" / "expected_sources.json"
    inventory_text = inventory_path.read_text()
    assert inventory_text == json.dumps(inv, indent=2, sort_keys=True) + "\n"


def test_initialise_run_omits_source_sha256(tmp_path: Path) -> None:
    fp_a = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=10, mtime_ns=12345)
    _run_dir, _state = initialise_run(tmp_path, run_id="r", expected_sources=[fp_a])
    inv = expected_source_inventory(tmp_path / "r")
    for entry in inv:
        assert "sha256" not in entry


def test_load_run_round_trips_metadata(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
    upsert_run_metadata(state, {"python": "3.12"})
    reloaded = load_run(run_dir)
    assert reloaded.metadata["python"] == "3.12"


def test_load_run_rejects_non_object_run_metadata(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")
    (run_dir / "manifests" / "run.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match=r"^run metadata must be a JSON object$"):
        load_run(run_dir)


def test_load_run_rejects_non_array_sources_manifest(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")
    (run_dir / "manifests" / "sources.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="sources manifest must be a JSON array"):
        load_run(run_dir)


def test_load_run_reports_malformed_json_with_manifest_context(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")
    (run_dir / "manifests" / "run.json").write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match=r"^invalid run metadata JSON: "):
        load_run(run_dir)


def test_load_run_reports_non_utf8_manifest_with_context(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")
    (run_dir / "manifests" / "sources.json").write_bytes(b"\xff")

    with pytest.raises(ValueError, match=r"^invalid sources manifest encoding: "):
        load_run(run_dir)


def test_load_run_rejects_duplicate_source_filenames(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")
    entries = [
        {"filename": "a-latest.osm.pbf", "size_bytes": 1, "mtime_ns": 2},
        {"filename": "a-latest.osm.pbf", "size_bytes": 3, "mtime_ns": 4},
    ]
    (run_dir / "manifests" / "sources.json").write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(ValueError, match="sources manifest contains duplicate filename"):
        load_run(run_dir)


def test_load_run_rejects_invalid_source_fingerprint(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")
    entry = {"filename": "a-latest.osm.pbf", "size_bytes": True, "mtime_ns": 2}
    (run_dir / "manifests" / "sources.json").write_text(json.dumps([entry]), encoding="utf-8")

    with pytest.raises(ValueError, match=r"sources manifest\[0\]\.size_bytes"):
        load_run(run_dir)


def test_expected_source_inventory_reports_a_missing_file_by_its_path(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"

    with pytest.raises(FileNotFoundError) as caught:
        expected_source_inventory(run_dir)

    assert str(caught.value) == f"missing {run_dir / 'manifests' / 'expected_sources.json'}"


def test_expected_source_inventory_names_the_expected_manifest_when_corrupt(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r"
    path = run_dir / "manifests" / "expected_sources.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        expected_source_inventory(run_dir)

    assert str(caught.value).startswith(f"invalid expected sources manifest JSON: {path}: ")


def test_source_inventory_matches_names_the_actual_manifest_when_corrupt(tmp_path: Path) -> None:
    fp = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=10, mtime_ns=12345)
    run_dir, _state = initialise_run(tmp_path, run_id="r", expected_sources=[fp])
    path = run_dir / "manifests" / "sources.json"
    path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        source_inventory_matches(run_dir)

    assert str(caught.value).startswith(f"invalid sources manifest JSON: {path}: ")


def test_expected_source_inventory_rejects_duplicate_filenames(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")
    entries = [
        {"filename": "a-latest.osm.pbf", "size_bytes": 1, "mtime_ns": 2},
        {"filename": "a-latest.osm.pbf", "size_bytes": 1, "mtime_ns": 2},
    ]
    (run_dir / "manifests" / "expected_sources.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="expected sources manifest contains duplicate filename"):
        expected_source_inventory(run_dir)


def test_source_fingerprint_captures_size_and_mtime(tmp_path: Path) -> None:
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    assert fp.filename == "monaco-latest.osm.pbf"
    assert fp.size_bytes == 4
    assert isinstance(fp.mtime_ns, int)


def test_source_fingerprint_changes_with_size(tmp_path: Path) -> None:
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"a")
    fp1 = snapshot_source_fingerprint(p)
    p.write_bytes(b"ab")
    fp2 = snapshot_source_fingerprint(p)
    assert fp1.size_bytes != fp2.size_bytes


def test_record_processed_source_appends(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
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


def test_update_source_enrichment_status_persists_deterministic_summary(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=10, observation_row_count=10)

    update_source_enrichment_status(
        state,
        filename=fp.filename,
        pending=True,
        status_counts={
            "website": {"fetch_error": 2, "success": 8},
            "contact_website": {"absent": 10},
        },
    )

    loaded = load_run(run_dir)
    assert loaded.sources[fp.filename]["enrichment_pending"] is True
    assert loaded.sources[fp.filename]["enrichment_status_counts"] == {
        "contact_website": {"absent": 10},
        "website": {"fetch_error": 2, "success": 8},
    }


@pytest.mark.parametrize(
    ("status", "count"),
    [(None, 1), ("success", True), ("success", -1), ("success", 1.5)],
)
def test_validated_status_count_rejects_malformed_values(
    status: object,
    count: object,
) -> None:
    message = r"^enrichment status counts must contain non-negative integers$"
    with pytest.raises(ValueError, match=message):
        _validated_status_count(status, count)


def test_validated_status_count_returns_the_count_it_checked() -> None:
    assert _validated_status_count("success", 3) == 3


def test_normalise_status_field_names_a_non_mapping_in_its_message() -> None:
    message = r"^enrichment status counts must be string-keyed mappings$"
    with pytest.raises(ValueError, match=message):
        _normalise_status_field("text", "not a mapping")


def test_record_processed_source_dedupes_by_filename(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=10, observation_row_count=10)
    record_processed_source(state, fp, public_row_count=11, observation_row_count=11)
    sources_path = run_dir / "manifests" / "sources.json"
    data = json.loads(sources_path.read_text())
    assert len(data) == 1
    assert data[0]["public_row_count"] == 11


def test_processed_sources_manifest_is_deterministic(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
    fp_b = SourceFingerprint(filename="b-latest.osm.pbf", size_bytes=8, mtime_ns=7)
    fp_a = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=2, mtime_ns=1)
    record_processed_source(
        state,
        fp_b,
        public_row_count=9,
        observation_row_count=10,
        rejection_count=11,
    )
    record_processed_source(
        state,
        fp_a,
        public_row_count=3,
        observation_row_count=4,
        rejection_count=5,
    )

    sources_path = run_dir / "manifests" / "sources.json"
    expected = [
        {
            "filename": "a-latest.osm.pbf",
            "mtime_ns": 1,
            "observation_row_count": 4,
            "public_row_count": 3,
            "rejection_count": 5,
            "size_bytes": 2,
            "status": "extracted",
        },
        {
            "filename": "b-latest.osm.pbf",
            "mtime_ns": 7,
            "observation_row_count": 10,
            "public_row_count": 9,
            "rejection_count": 11,
            "size_bytes": 8,
            "status": "extracted",
        },
    ]
    sources_text = sources_path.read_text()
    assert json.loads(sources_text) == expected
    assert sources_text == json.dumps(expected, indent=2, sort_keys=True) + "\n"


def test_run_metadata_persists_start_and_end(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
    upsert_run_metadata(state, {"started_at": "2024-01-01T00:00:00Z"})
    upsert_run_metadata(state, {"ended_at": "2024-01-01T00:01:00Z"})
    reloaded = load_run(run_dir)
    assert reloaded.metadata["started_at"] == "2024-01-01T00:00:00Z"
    assert reloaded.metadata["ended_at"] == "2024-01-01T00:01:00Z"


def test_upsert_run_metadata_rejects_status_change(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")
    with pytest.raises(ValueError, match="transition_status"):
        upsert_run_metadata(state, {"status": STATUS_COMPLETE})


def test_run_state_layout_does_not_create_unrequested_dirs(tmp_path: Path) -> None:
    parent_files = set(tmp_path.iterdir())
    initialise_run(tmp_path, run_id="abc")
    new_entries = set(tmp_path.iterdir()) - parent_files
    assert len(new_entries) == 1
    assert new_entries.pop().name == "abc"


def test_run_metadata_keys_are_sorted(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
    upsert_run_metadata(state, {"z": 1, "a": 2, "m": 3})
    run_json = (run_dir / "manifests" / "run.json").read_text()
    assert run_json.index('"a"') < run_json.index('"m"') < run_json.index('"z"')


def test_run_state_detects_source_mutation_via_size(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=10, observation_row_count=10)
    p.write_bytes(b"different")
    new_fp = snapshot_source_fingerprint(p)
    assert new_fp != fp
    assert source_is_unchanged(load_run(run_dir), new_fp) is False


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
    _run_dir, state = initialise_run(tmp_path, run_id="abc")
    assert state.metadata["status"] == STATUS_INITIALIZED


def test_transition_status_advances_through_pipeline(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")

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
    _run_dir, state = initialise_run(tmp_path, run_id="abc")

    with pytest.raises(ValueError, match="illegal"):
        transition_status(state, STATUS_COMPLETE)


def test_transition_to_incomplete_from_any_state(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")

    transition_status(state, STATUS_EXTRACTING)
    transition_status(state, STATUS_INCOMPLETE)
    assert state.metadata["status"] == STATUS_INCOMPLETE


def test_complete_state_rejects_generic_incomplete_transition(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")

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


def test_source_inventory_matches_rejects_duplicate_actual_entries(tmp_path: Path) -> None:
    fp_a = SourceFingerprint(filename="a-latest.osm.pbf", size_bytes=10, mtime_ns=12345)
    run_dir, _state = initialise_run(tmp_path, run_id="r", expected_sources=[fp_a])
    entries = [
        {"filename": "a-latest.osm.pbf", "size_bytes": 10, "mtime_ns": 12345},
        {"filename": "a-latest.osm.pbf", "size_bytes": 10, "mtime_ns": 12345},
    ]
    (run_dir / "manifests" / "sources.json").write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(ValueError, match="sources manifest contains duplicate filename"):
        source_inventory_matches(run_dir)


def test_atomic_write_json_writes_sorted_indented_text_and_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    atomic_write_json(target, {"b": 1, "a": [2]})

    assert target.read_text(encoding="utf-8") == '{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n'
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.json"]


def test_atomic_write_json_stages_a_same_directory_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged: list[str] = []
    original_replace = Path.replace

    def record_replace(self: Path, target: Path) -> Path:
        staged.append(self.name)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", record_replace)

    atomic_write_json(tmp_path / "state.json", {"a": 1})

    assert staged == ["state.json.tmp"]


def test_source_entry_messages_name_the_label_index_and_field() -> None:
    with pytest.raises(ValueError) as not_object:
        _validated_source_entry("a.osm.pbf", label="sources manifest", index=3)
    assert str(not_object.value) == "sources manifest[3] must be a JSON object"

    with pytest.raises(ValueError) as empty_name:
        _validate_source_filename("", label="sources manifest", index=2)
    assert str(empty_name.value) == "sources manifest[2].filename must be a non-empty string"

    with pytest.raises(ValueError) as entry_with_empty_name:
        _validated_source_entry({"filename": ""}, label="sources manifest", index=2)
    assert str(entry_with_empty_name.value) == (
        "sources manifest[2].filename must be a non-empty string"
    )


def test_source_inventory_matches_reports_corrupt_actual_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "manifests").mkdir(parents=True)
    (run_dir / "manifests" / "expected_sources.json").write_text("[]", encoding="utf-8")
    (run_dir / "manifests" / "sources.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid sources manifest JSON"):
        source_inventory_matches(run_dir)


def test_zero_status_count_is_accepted() -> None:
    _validated_status_count("success", 0)


def test_source_is_unchanged_requires_the_same_size_and_mtime(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="identity")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=1, observation_row_count=0)
    recorded = load_run(run_dir)

    assert source_is_unchanged(recorded, fp) is True
    assert (
        source_is_unchanged(
            recorded, SourceFingerprint(fp.filename, fp.size_bytes, fp.mtime_ns + 1)
        )
        is False
    )
    assert (
        source_is_unchanged(
            recorded, SourceFingerprint(fp.filename, fp.size_bytes + 1, fp.mtime_ns)
        )
        is False
    )
    assert (
        source_is_unchanged(
            recorded, SourceFingerprint("other.osm.pbf", fp.size_bytes, fp.mtime_ns)
        )
        is False
    )


def test_normalise_status_field_validates_and_sorts_counts() -> None:
    assert _normalise_status_field("text", {"success": 2, "absent": 0}) == {
        "absent": 0,
        "success": 2,
    }
    with pytest.raises(ValueError, match="string-keyed mappings"):
        _normalise_status_field(1, {"success": 1})
    with pytest.raises(ValueError, match="string-keyed mappings"):
        _normalise_status_field("text", ["success"])


def test_update_public_shard_metadata_records_the_shard_and_clears_pending(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="shard")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(state, fp, public_row_count=1, observation_row_count=0)
    state.sources[fp.filename]["enrichment_pending"] = True

    update_public_shard_metadata(state, filename=fp.filename, row_count=7, shard_sha256="a" * 64)

    entry = load_run(run_dir).sources[fp.filename]
    assert entry["public_row_count"] == 7
    assert entry["public_shard_sha256"] == "a" * 64
    assert "enrichment_pending" not in entry


def test_update_public_shard_metadata_rejects_an_unprocessed_source(tmp_path: Path) -> None:
    _, state = initialise_run(tmp_path, run_id="missing")

    with pytest.raises(ValueError, match=re.escape("source is not processed: ghost.osm.pbf")):
        update_public_shard_metadata(
            state, filename="ghost.osm.pbf", row_count=1, shard_sha256="b" * 64
        )


def test_initialise_run_creates_missing_output_parents(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path / "outputs" / "nested", run_id="abc")

    assert run_dir == tmp_path / "outputs" / "nested" / "abc"
    assert state.run_dir == run_dir


def test_initialise_run_records_id_creation_time_and_initial_status(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")

    stored = json.loads((run_dir / "manifests" / "run.json").read_text(encoding="utf-8"))
    assert stored == state.metadata
    assert stored["run_id"] == "abc"
    assert stored["status"] == STATUS_INITIALIZED
    assert datetime.fromisoformat(stored["created_at"]).tzinfo is not None


def test_initialise_run_writes_expected_sources_sorted_by_filename(tmp_path: Path) -> None:
    expected = [
        SourceFingerprint(filename="b.osm.pbf", size_bytes=2, mtime_ns=2),
        SourceFingerprint(filename="a.osm.pbf", size_bytes=1, mtime_ns=1),
    ]

    run_dir, _state = initialise_run(tmp_path, run_id="abc", expected_sources=expected)

    manifest = run_dir / "manifests" / "expected_sources.json"
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    assert [entry["filename"] for entry in stored] == ["a.osm.pbf", "b.osm.pbf"]


def test_load_run_prefers_the_recorded_run_id_over_the_directory_name(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="recorded")
    renamed = run_dir.rename(tmp_path / "renamed-directory")

    assert load_run(renamed).run_id == "recorded"


def test_load_run_falls_back_to_directory_name_when_run_id_absent(tmp_path: Path) -> None:
    run_dir = tmp_path / "fallback-run"
    (run_dir / "manifests").mkdir(parents=True)
    (run_dir / "manifests" / "run.json").write_text("{}", encoding="utf-8")

    assert load_run(run_dir).run_id == "fallback-run"


def test_load_run_reports_a_missing_run_json_by_its_path(tmp_path: Path) -> None:
    run_dir = tmp_path / "empty-run"

    with pytest.raises(FileNotFoundError) as missing:
        load_run(run_dir)

    assert str(missing.value) == f"missing {run_dir / 'manifests' / 'run.json'}"


def test_upsert_run_metadata_refuses_status_with_its_message(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")
    message = (
        "use transition_status() to change run status; "
        "upsert_run_metadata refuses to set it implicitly"
    )

    with pytest.raises(ValueError, match=rf"^{re.escape(message)}$"):
        upsert_run_metadata(state, {"status": STATUS_EXTRACTING})


def test_transition_status_walks_the_pipeline_and_records_each_change(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")

    for status in (STATUS_EXTRACTING, STATUS_EXTRACTED):
        transition_status(state, status)
        stored = json.loads((run_dir / "manifests" / "run.json").read_text(encoding="utf-8"))
        assert stored["status"] == status
        assert datetime.fromisoformat(stored["status_changed_at"]).tzinfo is not None

    assert state.metadata["status"] == STATUS_EXTRACTED


def test_record_processed_source_defaults_every_count_to_zero(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")
    pbf = _write_pbf_with_size(tmp_path / "monaco-latest.osm.pbf", b"data")
    fp = snapshot_source_fingerprint(pbf)

    record_processed_source(state, fp)

    entry = state.sources[fp.filename]
    counts = (entry["public_row_count"], entry["observation_row_count"], entry["rejection_count"])
    assert counts == (0, 0, 0)


def test_initialise_run_without_an_id_names_the_run_after_its_creation_time(
    tmp_path: Path,
) -> None:
    run_dir, state = initialise_run(tmp_path)

    assert state.run_id == run_dir.name
    assert re.fullmatch(r"\d{8}T\d{6}Z", run_dir.name)


def test_initialise_run_refuses_an_existing_run_directory(tmp_path: Path) -> None:
    initialise_run(tmp_path, run_id="abc")

    with pytest.raises(FileExistsError):
        initialise_run(tmp_path, run_id="abc")


def test_initialise_run_state_points_at_the_new_run(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="abc")

    assert state.run_dir == run_dir
    assert state.run_id == "abc"


def test_load_run_state_points_at_the_run_directory(tmp_path: Path) -> None:
    run_dir, _state = initialise_run(tmp_path, run_id="abc")

    assert load_run(run_dir).run_dir == run_dir


def test_transition_status_names_an_unknown_target_in_its_message(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")

    with pytest.raises(ValueError, match=r"^unknown run status: 'bogus'$"):
        transition_status(state, "bogus")


def test_a_run_without_a_recorded_status_starts_as_initialised(tmp_path: Path) -> None:
    run_dir = tmp_path / "statusless-run"
    (run_dir / "manifests").mkdir(parents=True)
    (run_dir / "manifests" / "run.json").write_text("{}", encoding="utf-8")
    state = load_run(run_dir)

    transition_status(state, STATUS_EXTRACTING)

    assert state.metadata["status"] == STATUS_EXTRACTING


def test_a_run_with_an_unknown_recorded_status_rejects_every_transition(tmp_path: Path) -> None:
    run_dir = tmp_path / "unknown-status-run"
    (run_dir / "manifests").mkdir(parents=True)
    (run_dir / "manifests" / "run.json").write_text('{"status": "bogus"}', encoding="utf-8")
    state = load_run(run_dir)

    with pytest.raises(
        ValueError, match=r"^illegal run-status transition: 'bogus' -> 'extracting'$"
    ):
        transition_status(state, STATUS_EXTRACTING)


def test_record_processed_source_keeps_timing_and_digests_it_is_given(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")
    pbf = _write_pbf_with_size(tmp_path / "monaco-latest.osm.pbf", b"data")
    fp = snapshot_source_fingerprint(pbf)

    record_processed_source(
        state,
        fp,
        started_at="start",
        finished_at="finish",
        public_shard_sha256="public",
        observation_shard_sha256="observation",
        rejection_shard_sha256="rejection",
    )

    entry = state.sources[fp.filename]
    assert entry["started_at"] == "start"
    assert entry["finished_at"] == "finish"
    assert entry["public_shard_sha256"] == "public"
    assert entry["observation_shard_sha256"] == "observation"
    assert entry["rejection_shard_sha256"] == "rejection"


def test_record_processed_source_omits_timing_and_digests_it_is_not_given(tmp_path: Path) -> None:
    _run_dir, state = initialise_run(tmp_path, run_id="abc")
    pbf = _write_pbf_with_size(tmp_path / "monaco-latest.osm.pbf", b"data")
    fp = snapshot_source_fingerprint(pbf)

    record_processed_source(state, fp)

    entry = state.sources[fp.filename]
    for key in (
        "started_at",
        "finished_at",
        "public_shard_sha256",
        "observation_shard_sha256",
        "rejection_shard_sha256",
    ):
        assert key not in entry


@pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="switching the process time zone needs tzset"
)
def test_default_run_id_is_the_utc_time_whatever_the_local_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    try:
        before = datetime.now(UTC).replace(microsecond=0)
        run_id = default_run_id()
        after = datetime.now(UTC)
    finally:
        monkeypatch.undo()
        time.tzset()

    assert before <= datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) <= after
