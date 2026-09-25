"""Tests for verify_results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import v1_2_polygon_row as _row

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.reporting import verify as verify_module
from osm_polygon_website_tag.reporting.verify import VerificationReport, verify_results
from osm_polygon_website_tag.runtime.run_state import (
    SourceManifestEntry,
    initialise_run,
    record_processed_source,
    snapshot_source_fingerprint,
    update_public_shard_metadata,
)


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _make_shard(run_dir: Path, *, stem: str, rows: list[dict[str, object]], kind: str) -> Path:
    """kind in {public, comparison, rejection}."""
    if kind == "public":
        schema = POLYGON_PUBLIC_SCHEMA
        parent = run_dir / "polygons"
    elif kind == "comparison":
        schema = COMPARISON_OBSERVATION_SCHEMA
        parent = run_dir / "analysis_observations"
    else:
        schema = REJECTION_SCHEMA
        parent = run_dir / "rejections"
    parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    p = parent / f"{stem}.parquet"
    pq.write_table(table, p, compression="snappy")
    return p


def _setup_minimal_run(tmp_path: Path, *, row_count: int = 1, manifest_row_count: int = 1):
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    rows = [
        _row(polygon_id="p1", region="monaco", source_pbf="monaco-latest.osm.pbf")
        for _ in range(row_count)
    ]
    shard = _make_shard(run_dir, stem="monaco-latest", rows=rows, kind="public")
    observation_shard = _make_shard(run_dir, stem="monaco-latest", rows=[], kind="comparison")
    rejection_shard = _make_shard(run_dir, stem="monaco-latest", rows=[], kind="rejection")
    public_sha = _sha256(shard)
    record_processed_source(
        state,
        fp,
        public_row_count=manifest_row_count,
        observation_row_count=0,
        rejection_count=0,
        public_shard_sha256=public_sha,
        observation_shard_sha256=_sha256(observation_shard),
        rejection_shard_sha256=_sha256(rejection_shard),
    )
    return run_dir, state


def _sha256(p: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def test_shared_json_loader_reports_parse_errors_once(tmp_path: Path) -> None:
    read_json_value = getattr(verify_module, "_read_json_value", None)
    assert callable(read_json_value)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json", encoding="utf-8")
    errors: list[str] = []
    ok, value = read_json_value(invalid, errors, label="array")
    assert ok is False
    assert value is None
    assert errors and errors[0].startswith("invalid JSON array")


@pytest.mark.parametrize(
    ("payload", "expected", "error_prefix"),
    [
        ([{"filename": "a.osm.pbf"}], [{"filename": "a.osm.pbf"}], None),
        ({"filename": "a.osm.pbf"}, [], "expected array of objects"),
        (["not an object"], [], "expected array of objects"),
    ],
)
def test_json_array_loader_accepts_only_arrays_of_objects(
    tmp_path: Path,
    payload: object,
    expected: list[dict[str, object]],
    error_prefix: str | None,
) -> None:
    path = tmp_path / "values.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    errors: list[str] = []

    result = verify_module._read_json_array(path, errors)

    assert result == expected
    if error_prefix is None:
        assert errors == []
    else:
        assert len(errors) == 1
        assert errors[0].startswith(error_prefix)


def test_json_array_loader_reports_missing_file(tmp_path: Path) -> None:
    errors: list[str] = []

    assert verify_module._read_json_array(tmp_path / "missing.json", errors) == []
    assert len(errors) == 1
    assert errors[0].startswith("invalid JSON array")


def _manifest_identity(
    *,
    filename: str = "source.osm.pbf",
    size_bytes: int = 10,
    mtime_ns: int = 20,
) -> SourceManifestEntry:
    return {"filename": filename, "size_bytes": size_bytes, "mtime_ns": mtime_ns}


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [("filename", "other.osm.pbf"), ("size_bytes", 11), ("mtime_ns", 21)],
)
def test_verify_expected_inventory_compares_every_identity_field(
    tmp_path: Path,
    field_name: str,
    changed_value: object,
) -> None:
    expected_entry = _manifest_identity()
    expected_path = tmp_path / "manifests" / "expected_sources.json"
    expected_path.parent.mkdir()
    expected_path.write_text(json.dumps([expected_entry]), encoding="utf-8")
    actual_entry = cast(SourceManifestEntry, dict(expected_entry))
    cast(dict[str, object], actual_entry)[field_name] = changed_value
    errors: list[str] = []

    verify_module._verify_expected_inventory(tmp_path, [actual_entry], errors)

    assert errors == ["processed sources do not exactly match expected source inventory"]


def test_read_json_value_uses_utf8_for_the_json_boundary() -> None:
    class PathSpy:
        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            return '{"value": 1}'

    errors: list[str] = []
    ok, value = verify_module._read_json_value(cast(Any, PathSpy()), errors, label="object")

    assert (ok, value, errors) == (True, {"value": 1}, [])


def test_verify_results_happy_path(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    report = verify_results(run_dir)
    assert isinstance(report, VerificationReport)
    assert report.ok is True
    assert report.errors == []


def test_verify_rejects_incorrect_website_word_count(tmp_path: Path) -> None:
    from osm_polygon_website_tag.runtime.run_state import update_public_shard_metadata

    run_dir, state = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    rows = pq.read_table(shard).to_pylist()
    rows[0]["website_word_count"] = 99
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), shard)
    update_public_shard_metadata(
        state,
        filename="monaco-latest.osm.pbf",
        row_count=1,
        shard_sha256=_sha256(shard),
    )

    report = verify_results(run_dir)

    assert not report.ok
    assert any("word count" in error for error in report.errors)


@pytest.mark.parametrize(
    ("text", "word_count", "valid"),
    [("", 0, True), ("not empty", 0, False), ("", 1, False)],
)
def test_verify_empty_text_status_requires_empty_text_and_zero_words(
    tmp_path: Path,
    *,
    text: str,
    word_count: int,
    valid: bool,
) -> None:
    run_dir, state = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    rows = pq.read_table(shard).to_pylist()
    rows[0]["website_text"] = text
    rows[0]["website_word_count"] = word_count
    rows[0]["website_text_status"] = "empty"
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), shard)
    update_public_shard_metadata(
        state,
        filename="monaco-latest.osm.pbf",
        row_count=1,
        shard_sha256=_sha256(shard),
    )

    report = verify_results(run_dir)

    assert report.ok is valid


def test_verify_results_rejects_modified_shard(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    new = table.to_pylist()
    new[0]["website"] = "https://tampered.com"
    pq.write_table(pa.Table.from_pylist(new, schema=table.schema), shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("mismatch" in e or "count" in e or "modified" in e for e in report.errors)


@pytest.mark.parametrize(
    ("kind", "directory"),
    [
        ("public", "polygons"),
        ("comparison", "analysis_observations"),
        ("rejection", "rejections"),
    ],
)
def test_verify_rejects_modified_shard_hash(tmp_path: Path, *, kind: str, directory: str) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / directory / "monaco-latest.parquet"
    table = pq.read_table(shard)
    # Rewrite with different compression: bytes change, schema and rows do not.
    pq.write_table(table, shard, compression="gzip")

    report = verify_results(run_dir)

    assert report.ok is False
    assert any(kind in error and "shard hash mismatch" in error for error in report.errors), (
        f"expected '{kind}' and 'shard hash mismatch' in errors, got: {report.errors}"
    )


def test_verify_results_rejects_missing_shard(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(
        state, fp, public_row_count=1, observation_row_count=0, rejection_count=0
    )
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("missing" in e.lower() for e in report.errors)


def test_verify_results_rejects_extra_shard(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    record_processed_source(
        state, fp, public_row_count=1, observation_row_count=0, rejection_count=0
    )
    rows = [_row(polygon_id="p1", region="monaco", source_pbf="monaco-latest.osm.pbf")]
    _make_shard(run_dir, stem="monaco-latest", rows=rows, kind="public")
    _make_shard(run_dir, stem="monaco-latest", rows=[], kind="comparison")
    _make_shard(run_dir, stem="monaco-latest", rows=[], kind="rejection")
    # Add rogue shard:
    _make_shard(run_dir, stem="rogue", rows=rows, kind="public")
    _make_shard(run_dir, stem="rogue", rows=[], kind="comparison")
    _make_shard(run_dir, stem="rogue", rows=[], kind="rejection")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("extra" in e.lower() or "undeclared" in e.lower() for e in report.errors)


def test_verify_results_rejects_schema_drift(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard).drop(["name"])
    pq.write_table(table, shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("schema" in e.lower() for e in report.errors)


def test_verify_results_rejects_row_count_mismatch(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path, row_count=1, manifest_row_count=5)
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("row" in e.lower() for e in report.errors)


def test_verify_results_rejects_empty_manifest(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    rows = [_row(polygon_id="p1", region="monaco", source_pbf="monaco-latest.osm.pbf")]
    _make_shard(run_dir, stem="monaco-latest", rows=rows, kind="public")
    report = verify_results(run_dir)
    assert report.ok is False
    assert any("manifest" in e.lower() or "empty" in e.lower() for e in report.errors)


def test_verify_results_rejects_nan_coordinates(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    new = table.to_pylist()
    new[0]["lat"] = float("nan")
    pq.write_table(pa.Table.from_pylist(new, schema=table.schema), shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False


def test_verify_results_rejects_negative_area(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    table = pq.read_table(shard)
    new = table.to_pylist()
    new[0]["area_m2"] = -1.0
    pq.write_table(pa.Table.from_pylist(new, schema=table.schema), shard, compression="snappy")
    report = verify_results(run_dir)
    assert report.ok is False


def test_verify_results_rejects_corrupt_manifest(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    (run_dir / "manifests" / "sources.json").write_text("{not-json")
    report = verify_results(run_dir)
    assert report.ok is False


def test_verify_results_reports_non_utf8_manifest(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    (run_dir / "manifests" / "sources.json").write_bytes(b"\xff")

    report = verify_results(run_dir)

    assert report.ok is False
    assert any("invalid JSON array" in error for error in report.errors)


def test_verify_results_rejects_empty_run_metadata(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    (run_dir / "manifests" / "run.json").write_text("{}", encoding="utf-8")

    report = verify_results(run_dir)

    assert report.ok is False
    assert "run metadata is empty" in report.errors


def _set_status(run_dir: Path, status: str) -> None:
    path = run_dir / "manifests" / "run.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["status"] = status
    path.write_text(json.dumps(metadata), encoding="utf-8")


def _rewrite_public_row(
    run_dir: Path, state: Any, schema: pa.Schema = POLYGON_PUBLIC_SCHEMA, **changes: object
) -> None:
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    rows = pq.read_table(shard).to_pylist()
    rows[0].update(changes)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), shard)
    update_public_shard_metadata(
        state, filename="monaco-latest.osm.pbf", row_count=1, shard_sha256=_sha256(shard)
    )


VERIFIERS = {
    "strict": verify_module.verify_results,
    "modern": verify_module.verify_results_modern,
    "release": verify_module.verify_release_results,
}


@pytest.mark.parametrize("verifier", sorted(VERIFIERS))
@pytest.mark.parametrize("status", ["extracted", "card_built", "verified", "complete"])
def test_status_selects_the_card_release_and_receipt_contracts(
    tmp_path: Path, verifier: str, status: str
) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    _set_status(run_dir, status)

    errors = VERIFIERS[verifier](run_dir).errors

    card_checked = status != "extracted"
    assert ("missing card artifact: README.md" in errors) is card_checked
    release_checked = card_checked and verifier == "release"
    assert any(e.startswith("README geometry section is unreadable") for e in errors) is (
        release_checked
    )
    receipt_checked = status == "complete" and verifier != "modern"
    assert ("completion receipt has no artifact list" in errors) is receipt_checked


def test_verify_results_reports_the_empty_sources_manifest_exactly(tmp_path: Path) -> None:
    run_dir, _ = initialise_run(tmp_path, run_id="r")

    assert "sources manifest is empty" in verify_results(run_dir).errors


def test_verify_results_reports_corrupt_run_metadata_as_an_invalid_object(
    tmp_path: Path,
) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    run_json = run_dir / "manifests" / "run.json"
    run_json.write_text("{not-json", encoding="utf-8")

    errors = verify_results(run_dir).errors

    assert any(e.startswith(f"invalid JSON object {run_json}: ") for e in errors)
    assert "run metadata is empty" in errors


def test_verify_results_rejects_a_mismatched_expected_inventory(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    expected = [_manifest_identity(filename="other.osm.pbf")]
    (run_dir / "manifests" / "expected_sources.json").write_text(
        json.dumps(expected), encoding="utf-8"
    )

    errors = verify_results(run_dir).errors

    assert "processed sources do not exactly match expected source inventory" in errors


@pytest.mark.parametrize("verifier", sorted(VERIFIERS))
def test_every_verifier_rejects_text_left_pending_after_completion(
    tmp_path: Path, verifier: str
) -> None:
    run_dir, state = _setup_minimal_run(tmp_path)
    _rewrite_public_row(
        run_dir, state, website_text=None, website_word_count=None, website_text_status="pending"
    )
    _set_status(run_dir, "complete")

    errors = VERIFIERS[verifier](run_dir).errors

    assert "monaco-latest.parquet:website remains pending after enrichment" in errors


def test_release_verification_rejects_bad_text_and_language_fields(tmp_path: Path) -> None:
    run_dir, state = _setup_minimal_run(tmp_path)
    _rewrite_public_row(
        run_dir,
        state,
        POLYGON_PUBLIC_SCHEMA_V1_4,
        website_word_count=99,
        website_language="en",
        website_language_probability=2.0,
    )

    errors = verify_module.verify_release_results(run_dir).errors

    assert "monaco-latest.parquet:website word count does not match stored text" in errors
    assert any(e.endswith("row 0 website language probability is invalid") for e in errors)


def test_verify_results_reports_a_corrupt_expected_inventory(tmp_path: Path) -> None:
    run_dir, _ = _setup_minimal_run(tmp_path)
    expected = run_dir / "manifests" / "expected_sources.json"
    expected.write_text("{not-json", encoding="utf-8")

    errors = verify_results(run_dir).errors

    assert any(e.startswith(f"invalid JSON array {expected}: ") for e in errors)
