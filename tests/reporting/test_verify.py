"""Tests for verify_results."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast, get_type_hints

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.reporting import verify as verify_module
from osm_polygon_website_tag.reporting.verification import shards as shards_module
from osm_polygon_website_tag.reporting.verify import VerificationReport, verify_results
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_COMPLETE,
    SourceManifestEntry,
    initialise_run,
    record_processed_source,
    snapshot_source_fingerprint,
    update_public_shard_metadata,
)


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row(
    *,
    polygon_id: str,
    region: str,
    source_pbf: str,
    lat: float = 0.0,
    lon: float = 0.0,
    area_m2: float = 50.0,
    website: str = "https://example.com",
    contact_website: str | None = None,
    has_website: bool = True,
    has_contact_website: bool = False,
    has_any_website: bool = True,
    preferred_website: str = "https://example.com",
    preferred_website_source: str = "website",
    website_class: str = "absolute_url",
    contact_website_class: str | None = None,
    website_hostname: str = "example.com",
    contact_website_hostname: str | None = None,
    wikidata: str | None = "Q42",
    wikidata_qid: str | None = "Q42",
    wikidata_class: str | None = "canonical_qid",
    name: str | None = None,
    tags: str = "{}",
    tag_keys: str = "[]",
    tag_count: int = 0,
    osm_primary_tag: str = "building",
    geometry: str = json.dumps({"type": "Polygon", "coordinates": []}),
    centroid: str = json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
    bbox: str = "[0.0,0.0,0.0,0.0]",
    area_km2: float = 5e-5,
    area_bucket: str = "10-100m2",
    centroid_kind: str = "lambert_azimuthal_equal_area",
    schema_version: str = "v1.2",
    website_text: str | None = "example text",
    website_word_count: int | None = 2,
    website_text_status: str = "success",
    contact_website_text: str | None = None,
    contact_website_word_count: int | None = None,
    contact_website_text_status: str = "absent",
) -> dict[str, object]:
    return {
        "polygon_id": polygon_id,
        "region": region,
        "source_pbf": source_pbf,
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "website": website,
        "contact_website": contact_website,
        "has_website": has_website,
        "has_contact_website": has_contact_website,
        "has_any_website": has_any_website,
        "preferred_website": preferred_website,
        "preferred_website_source": preferred_website_source,
        "website_class": website_class,
        "contact_website_class": contact_website_class,
        "website_hostname": website_hostname,
        "contact_website_hostname": contact_website_hostname,
        "wikidata": wikidata,
        "wikidata_qid": wikidata_qid,
        "wikidata_class": wikidata_class,
        "name": name,
        "tags": tags,
        "tag_keys": tag_keys,
        "tag_count": tag_count,
        "osm_primary_tag": osm_primary_tag,
        "geometry": geometry,
        "centroid": centroid,
        "lat": lat,
        "lon": lon,
        "bbox": bbox,
        "area_m2": area_m2,
        "area_km2": area_km2,
        "area_bucket": area_bucket,
        "centroid_kind": centroid_kind,
        "schema_version": schema_version,
        "website_text": website_text,
        "website_word_count": website_word_count,
        "website_text_status": website_text_status,
        "contact_website_text": contact_website_text,
        "contact_website_word_count": contact_website_word_count,
        "contact_website_text_status": contact_website_text_status,
    }


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


def test_verify_results_modern_forwards_the_receipt_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, bool]] = []
    expected = VerificationReport(True)

    def verify(root: Path, *, include_receipt: bool) -> VerificationReport:
        calls.append((root, include_receipt))
        return expected

    monkeypatch.setattr(verify_module, "_verify_results", verify)

    assert verify_module.verify_results_modern(str(tmp_path)) is expected
    assert calls == [(tmp_path, False)]


def test_verify_results_wires_all_checks_to_one_error_and_checked_accumulator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest: list[SourceManifestEntry] = [
        {"filename": "source.osm.pbf", "size_bytes": 1, "mtime_ns": 2}
    ]
    errors_ref: list[str] | None = None
    checked_ref: list[str] | None = None
    calls: list[str] = []

    def require_errors(errors: list[str]) -> None:
        nonlocal errors_ref
        assert isinstance(errors, list)
        if errors_ref is None:
            errors_ref = errors
        else:
            assert errors is errors_ref

    def read_object(path: Path, errors: list[str]) -> dict[str, Any]:
        require_errors(errors)
        assert path == tmp_path / "manifests" / "run.json"
        calls.append("metadata")
        return {"status": STATUS_COMPLETE}

    def read_manifest(path: Path, errors: list[str]) -> list[SourceManifestEntry]:
        require_errors(errors)
        assert path == tmp_path / "manifests" / "sources.json"
        calls.append("manifest")
        return manifest

    def verify_shards(
        root: Path,
        actual_manifest: list[SourceManifestEntry],
        errors: list[str],
        checked: list[str],
    ) -> None:
        nonlocal checked_ref
        require_errors(errors)
        assert root == tmp_path
        assert actual_manifest == manifest
        checked_ref = checked
        checked.append("source.osm.pbf")
        calls.append("shards")

    def verify_expected(
        root: Path, actual_manifest: list[SourceManifestEntry], errors: list[str]
    ) -> None:
        require_errors(errors)
        assert (root, actual_manifest) == (tmp_path, manifest)
        calls.append("expected")

    def verify_rows(root: Path, errors: list[str]) -> None:
        require_errors(errors)
        assert root == tmp_path
        calls.append("rows")

    def verify_text(root: Path, status: object, errors: list[str]) -> None:
        require_errors(errors)
        assert (root, status) == (tmp_path, STATUS_COMPLETE)
        calls.append("text")

    def verify_language(root: Path, errors: list[str]) -> None:
        require_errors(errors)
        assert root == tmp_path
        calls.append("language")

    def verify_status(root: Path, status: object, include_receipt: bool, errors: list[str]) -> None:
        require_errors(errors)
        assert (root, status, include_receipt) == (tmp_path, STATUS_COMPLETE, True)
        calls.append("status")

    monkeypatch.setattr(verify_module, "_read_json_object", read_object)
    monkeypatch.setattr(verify_module, "_read_source_manifest", read_manifest)
    monkeypatch.setattr(verify_module, "_verify_shards", verify_shards)
    monkeypatch.setattr(verify_module, "_verify_expected_inventory", verify_expected)
    monkeypatch.setattr(verify_module, "_verify_row_invariants", verify_rows)
    monkeypatch.setattr(verify_module, "_verify_text_invariants", verify_text)
    monkeypatch.setattr(verify_module, "_verify_language_invariants", verify_language)
    monkeypatch.setattr(verify_module, "_verify_status_artifacts", verify_status)

    report = verify_module._verify_results(tmp_path, include_receipt=True)

    assert report == VerificationReport(True, [], ["source.osm.pbf"])
    assert calls == [
        "metadata",
        "manifest",
        "shards",
        "expected",
        "rows",
        "text",
        "language",
        "status",
    ]
    assert checked_ref is report.checked_shards


def test_verify_results_uses_stable_errors_for_empty_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_module, "_read_json_object", lambda _path, _errors: {})
    monkeypatch.setattr(verify_module, "_read_source_manifest", lambda _path, _errors: [])
    monkeypatch.setattr(verify_module, "_verify_shards", lambda *_args: None)
    monkeypatch.setattr(verify_module, "_verify_expected_inventory", lambda *_args: None)
    monkeypatch.setattr(verify_module, "_verify_row_invariants", lambda *_args: None)
    monkeypatch.setattr(verify_module, "_verify_text_invariants", lambda *_args: None)
    monkeypatch.setattr(verify_module, "_verify_language_invariants", lambda *_args: None)
    monkeypatch.setattr(verify_module, "_verify_status_artifacts", lambda *_args: None)

    report = verify_module._verify_results(tmp_path, include_receipt=False)

    assert report.errors == ["sources manifest is empty", "run metadata is empty"]


def test_verify_status_artifacts_selects_the_exact_status_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path, list[str]]] = []
    errors: list[str] = []

    def verify_analysis(root: Path, errors_value: list[str]) -> None:
        calls.append(("analysis", root, errors_value))

    def verify_receipt(root: Path, errors_value: list[str]) -> None:
        calls.append(("receipt", root, errors_value))

    monkeypatch.setattr(verify_module, "_verify_analysis_and_card", verify_analysis)
    monkeypatch.setattr(verify_module, "_verify_receipt", verify_receipt)

    for status in ("card_built", "verified", "complete", "analyzed"):
        verify_module._verify_status_artifacts(tmp_path, status, True, errors)
    verify_module._verify_status_artifacts(tmp_path, "complete", False, errors)

    assert calls == [
        ("analysis", tmp_path, errors),
        ("analysis", tmp_path, errors),
        ("analysis", tmp_path, errors),
        ("receipt", tmp_path, errors),
        ("analysis", tmp_path, errors),
    ]


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
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    changed_value: object,
) -> None:
    expected_path = tmp_path / "manifests" / "expected_sources.json"
    expected_path.parent.mkdir()
    expected_path.write_text("[]")
    expected_entry = _manifest_identity()
    actual_entry = cast(SourceManifestEntry, dict(expected_entry))
    cast(dict[str, object], actual_entry)[field_name] = changed_value
    errors: list[str] = []

    def read_manifest(path: Path, errors_value: list[str]) -> list[SourceManifestEntry]:
        assert path == expected_path
        assert errors_value is errors
        return [expected_entry]

    monkeypatch.setattr(verify_module, "_read_source_manifest", read_manifest)

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


def test_read_json_object_forwards_error_list_and_object_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[str] = []
    calls: list[tuple[Path, list[str], str]] = []

    def read_value(path: Path, errors_value: list[str], *, label: str) -> tuple[bool, Any]:
        calls.append((path, errors_value, label))
        return True, {"status": STATUS_COMPLETE}

    monkeypatch.setattr(verify_module, "_read_json_value", read_value)

    assert verify_module._read_json_object(tmp_path / "run.json", errors) == {
        "status": STATUS_COMPLETE
    }
    assert calls == [(tmp_path / "run.json", errors, "object")]


def test_reporting_manifest_consumers_use_typed_entries() -> None:
    assert (
        get_type_hints(verify_module._verify_expected_inventory)["manifest"]
        == list[SourceManifestEntry]
    )
    assert get_type_hints(shards_module.verify_shards)["manifest"] == list[SourceManifestEntry]


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


def test_verify_results_math_isfinite_helper() -> None:
    assert math.isfinite(1.0)
    assert not math.isfinite(float("inf"))
    assert not math.isfinite(float("nan"))
