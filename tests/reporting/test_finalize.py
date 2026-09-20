"""Tests for finalize_run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.reporting.finalize as finalize_module
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.finalize import (
    _column_has_unfinished_status,
    _snapshot_preflight_error,
    _unfinished_shard_error,
    finalize_run,
    finalize_snapshot,
    replace_receipt_atomic,
)
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    STATUS_VERIFIED,
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
    transition_status,
    upsert_run_metadata,
)


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row(
    *, polygon_id: str = "p1", region: str = "monaco", source_pbf: str = "monaco-latest.osm.pbf"
):
    return {
        "polygon_id": polygon_id,
        "region": region,
        "source_pbf": source_pbf,
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "website": "https://example.com",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "preferred_website": "https://example.com",
        "preferred_website_source": "website",
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.com",
        "contact_website_hostname": None,
        "wikidata": "Q42",
        "wikidata_qid": "Q42",
        "wikidata_class": "canonical_qid",
        "name": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": "building",
        "geometry": json.dumps({"type": "Polygon", "coordinates": []}),
        "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0.0,0.0,0.0,0.0]",
        "area_m2": 50.0,
        "area_km2": 5e-5,
        "area_bucket": "10-100m2",
        "centroid_kind": "lambert_azimuthal_equal_area",
        "schema_version": "v1.2",
        "website_text": "example text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _setup(tmp_path: Path) -> tuple[Path, object]:
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    run_dir, state = initialise_run(tmp_path, run_id="r", expected_sources=[fp])
    rows = [_row()]
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pub.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), pub, compression="snappy"
    )
    obs = run_dir / "analysis_observations" / "monaco-latest.parquet"
    obs.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA),
        obs,
        compression="snappy",
    )
    rej = run_dir / "rejections" / "monaco-latest.parquet"
    rej.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([], schema=REJECTION_SCHEMA), rej, compression="snappy")
    record_processed_source(
        state,
        fp,
        public_row_count=1,
        observation_row_count=0,
        rejection_count=0,
        public_shard_sha256=_sha(pub),
        observation_shard_sha256=_sha(obs),
        rejection_shard_sha256=_sha(rej),
    )
    # Walk through the state machine.
    transition_status(state, STATUS_EXTRACTING)
    transition_status(state, STATUS_EXTRACTED)
    transition_status(state, STATUS_ENRICHING)
    transition_status(state, STATUS_ENRICHED)
    analyze_results(run_dir)
    transition_status(state, STATUS_ANALYZED)
    build_card(run_dir)
    transition_status(state, STATUS_CARD_BUILT)
    return run_dir, state


def _setup_frozen_extraction(
    tmp_path: Path,
    *,
    website_status: str = "success",
) -> tuple[Path, object]:
    """Create a complete extracted bundle with an owner freeze marker."""
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    run_dir, state = initialise_run(tmp_path, run_id="frozen", expected_sources=[fp])
    row = _row()
    if website_status != "success":
        row["website_text"] = None
        row["website_word_count"] = None
        row["website_text_status"] = website_status
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA), pub)
    obs = run_dir / "analysis_observations" / "monaco-latest.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA), obs)
    rej = run_dir / "rejections" / "monaco-latest.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=REJECTION_SCHEMA), rej)
    record_processed_source(
        state,
        fp,
        public_row_count=1,
        observation_row_count=0,
        rejection_count=0,
        public_shard_sha256=_sha(pub),
        observation_shard_sha256=_sha(obs),
        rejection_shard_sha256=_sha(rej),
    )
    transition_status(state, STATUS_EXTRACTING)
    upsert_run_metadata(state, {"snapshot_status": "done"})
    return run_dir, state


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_finalize_private_status_helpers_detect_pending_rows(tmp_path: Path) -> None:
    path = tmp_path / "shard.parquet"
    pq.write_table(
        pa.table(
            {
                "website_text_status": pa.array(["success", "pending"]),
                "contact_website_text_status": pa.array(["absent", "absent"]),
            }
        ),
        path,
    )
    parquet = pq.ParquetFile(path)
    assert _column_has_unfinished_status(parquet, "website_text_status")
    assert not _column_has_unfinished_status(parquet, "contact_website_text_status")
    assert _unfinished_shard_error(path) == "shard.parquet contains unfinished text statuses"
    assert _snapshot_preflight_error({"snapshot_status": "done"}, "card_built") is None
    assert (
        _snapshot_preflight_error({}, "initialized")
        == "snapshot finalization requires snapshot_status='done'"
    )


def test_failed_snapshot_report_is_a_failed_verification_report() -> None:
    report = finalize_module._failed_snapshot_report("bad snapshot")

    assert report.ok is False
    assert report.verification.ok is False
    assert report.verification.errors == ["bad snapshot"]


def test_unfinished_text_status_scan_uses_the_canonical_polygon_directory(
    tmp_path: Path,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    pq.write_table(
        pa.table(
            {
                "website_text_status": pa.array(["pending"]),
                "contact_website_text_status": pa.array(["absent"]),
            }
        ),
        polygons / "pending.parquet",
    )

    assert finalize_module._unfinished_text_status_errors(tmp_path) == [
        "pending.parquet contains unfinished text statuses"
    ]


def test_unfinished_text_status_scan_passes_the_canonical_directory_name() -> None:
    requested: list[str] = []

    class Directory:
        def glob(self, pattern: str) -> list[Path]:
            assert pattern == "*.parquet"
            return []

    class Root:
        def __truediv__(self, name: str) -> Directory:
            requested.append(name)
            return Directory()

    assert finalize_module._unfinished_text_status_errors(cast(Path, Root())) == []
    assert requested == ["polygons"]


def test_completion_receipt_uses_canonical_case_sensitive_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "sources.json").write_bytes(b"[]\n")
    map_path = tmp_path / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir()
    map_path.write_bytes(b"png")
    (tmp_path / "stats.json").write_bytes(b"{}\n")

    yaml_calls: list[Path] = []
    readme_calls: list[Path] = []
    source_reads: list[Path] = []
    is_file_calls: list[Path] = []
    replace_targets: list[Path] = []

    def yaml_hash(path: Path) -> str:
        yaml_calls.append(path)
        return "yaml-digest"

    def readme_hash(path: Path) -> str:
        readme_calls.append(path)
        return "readme-digest"

    real_read_bytes = Path.read_bytes
    real_is_file = Path.is_file
    real_replace = Path.replace

    def read_bytes(path: Path) -> bytes:
        source_reads.append(path)
        return real_read_bytes(path)

    def is_file(path: Path) -> bool:
        is_file_calls.append(path)
        return real_is_file(path)

    def replace(path: Path, target: Path) -> Path:
        replace_targets.append(target)
        return real_replace(path, target)

    monkeypatch.setattr(finalize_module, "publishable_paths", lambda _root: [])
    monkeypatch.setattr(finalize_module, "data_manifest_sha256", lambda _root: "data-digest")
    monkeypatch.setattr(finalize_module, "text_population_manifest_entries", lambda _root: ())
    monkeypatch.setattr(finalize_module, "yaml_custom_sha256", yaml_hash)
    monkeypatch.setattr(finalize_module, "readme_preserved_sha256", readme_hash)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "replace", replace)

    receipt = finalize_module._write_completion_receipt(tmp_path)

    assert receipt["card_contract_version"] == 2
    assert yaml_calls == [tmp_path / "dataset.yaml", tmp_path / "README.md"]
    assert readme_calls == [tmp_path / "README.md"]
    assert source_reads == [tmp_path / "manifests" / "sources.json"]
    assert tmp_path / "stats.json" in is_file_calls
    assert replace_targets == [tmp_path / "manifests" / "completion_receipt.json"]
    assert (manifests / "completion_receipt.json").is_file()


def test_finalize_run_writes_receipt(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    report = finalize_run(run_dir)
    assert report.ok is True
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text())
    raw_receipt = receipt_path.read_bytes()
    assert raw_receipt.startswith(b'{\n  "artifacts":')
    assert receipt["schema_version"] == "v1.2"
    assert receipt["digest_algorithm"] == "sha256"
    assert "manifest_digest" in receipt
    assert "data_manifest_sha256" in receipt
    assert "dataset_yaml_custom_sha256" in receipt
    assert "readme_yaml_custom_sha256" in receipt
    assert "text_population_manifest" in receipt
    assert receipt["sources_count"] == 1
    paths = {entry["path"] for entry in receipt["artifacts"]}
    assert "README.md" in paths
    assert "analysis/cells_global.parquet" in paths
    assert "polygons/monaco-latest.parquet" in paths
    assert receipt["card_contract_version"] == 2
    assert "assets/geographic_polygon_density.png" in paths


def test_v12_receipt_without_data_manifest_remains_compatible(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    assert finalize_run(run_dir).ok

    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "v1.2"
    del receipt["data_manifest_sha256"]
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = verify_results(run_dir)

    assert report.ok, report.errors


def test_finalize_snapshot_finishes_existing_data_without_enrichment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir, _ = _setup_frozen_extraction(tmp_path, website_status="fetch_error")
    monkeypatch.setattr(
        "osm_polygon_website_tag.pipeline.enrich.enrich_polygon_shard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("enrichment ran")),
    )

    report = finalize_snapshot(run_dir)

    assert report.ok is True, report.verification.errors
    state = load_run(run_dir)
    assert state.metadata["status"] == STATUS_COMPLETE
    assert state.metadata["snapshot_status"] == "done"
    assert (run_dir / "analysis" / "cells_global.parquet").is_file()
    assert (run_dir / "manifests" / "completion_receipt.json").is_file()


def test_finalize_snapshot_rejects_unfinished_text_rows(tmp_path: Path) -> None:
    run_dir, _ = _setup_frozen_extraction(tmp_path, website_status="pending")

    report = finalize_snapshot(run_dir)

    assert report.ok is False
    assert report.receipt == {}
    assert report.verification.ok is False
    assert report.verification.checked_shards
    assert any("unfinished text statuses" in error for error in report.verification.errors)
    assert not (run_dir / "manifests" / "completion_receipt.json").exists()


def test_finalize_snapshot_rejects_non_frozen_runs_before_verification(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)

    report = finalize_snapshot(run_dir)

    assert report.ok is False
    assert report.receipt == {}
    assert report.verification.errors == ["snapshot finalization requires snapshot_status='done'"]


def test_finalize_run_transitions_to_complete(tmp_path: Path) -> None:
    run_dir, _state = _setup(tmp_path)
    finalize_run(run_dir)
    state = load_run(run_dir)
    assert state.metadata["status"] == STATUS_COMPLETE


def test_finalize_run_receipt_digest_is_stable(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    r1 = finalize_run(run_dir)
    # Re-finalize -- receipt is rewritten deterministically.
    r2 = finalize_run(run_dir)
    assert r1.receipt["manifest_digest"] == r2.receipt["manifest_digest"]


def test_finalize_run_fails_on_verification_error(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    # Corrupt the shard.
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    shard.write_bytes(b"not parquet")
    report = finalize_run(run_dir)
    assert report.ok is False
    assert report.receipt == {}
    assert report.verification.ok is False


def test_finalize_run_can_proceed_to_complete(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    finalize_run(run_dir)
    state = load_run(run_dir)
    assert state.metadata["status"] == STATUS_COMPLETE


def test_complete_verification_rejects_card_mutation(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    assert finalize_run(run_dir).ok
    (run_dir / "README.md").write_text("tampered")

    report = verify_results(run_dir)

    assert not report.ok
    assert any("README" in error or "receipt-bound" in error for error in report.errors)


def test_complete_verification_rejects_analysis_mutation(tmp_path: Path) -> None:
    run_dir, _ = _setup(tmp_path)
    assert finalize_run(run_dir).ok
    path = run_dir / "analysis" / "cells_global.parquet"
    path.write_bytes(path.read_bytes() + b"tampered")

    report = verify_results(run_dir)

    assert not report.ok
    assert any("analysis" in error or "receipt-bound" in error for error in report.errors)


@pytest.mark.parametrize("remove_map", [False, True])
def test_complete_verification_requires_card_contract(
    tmp_path: Path,
    *,
    remove_map: bool,
) -> None:
    run_dir, _ = _setup(tmp_path)
    assert finalize_run(run_dir).ok
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.pop("card_contract_version")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    if remove_map:
        (run_dir / "assets" / "geographic_polygon_density.png").unlink()

    report = verify_results(run_dir)

    assert not report.ok
    assert any("card_contract_version" in error for error in report.errors)


@pytest.mark.parametrize("missing", ["map", "stats"])
def test_completion_receipt_binds_card_contract_to_both_generated_artifacts(
    tmp_path: Path,
    *,
    missing: str,
) -> None:
    run_dir, _ = _setup(tmp_path)
    assert finalize_run(run_dir).ok
    if missing == "map":
        (run_dir / "assets" / "geographic_polygon_density.png").unlink()
    else:
        (run_dir / "stats.json").unlink()

    receipt = finalize_module._write_completion_receipt(run_dir)

    assert "card_contract_version" not in receipt


def test_finalize_private_status_scan_is_bounded_and_fail_closed() -> None:
    calls: list[dict[str, object]] = []

    class Batch:
        def __init__(self, values: list[object]) -> None:
            self.values = values

        def column(self, name: str):
            assert name == "website_text_status"
            return type("Column", (), {"to_pylist": lambda _self: self.values})()

    class Parquet:
        def iter_batches(self, **kwargs: object):
            calls.append(kwargs)
            yield Batch(["success"])
            yield Batch([None])

    assert _column_has_unfinished_status(cast(pq.ParquetFile, Parquet()), "website_text_status")
    assert calls == [{"columns": ["website_text_status"], "batch_size": 8_192}]


def test_finalize_snapshot_state_advances_only_expected_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = type("State", (), {"metadata": {"status": STATUS_EXTRACTING}})()
    transitions: list[str] = []
    actions: list[str] = []

    def transition(_state: object, status: str) -> None:
        transitions.append(status)
        state.metadata["status"] = status

    monkeypatch.setattr(finalize_module, "transition_status", transition)
    monkeypatch.setattr(finalize_module, "analyze_results", lambda _root: actions.append("analyze"))
    monkeypatch.setattr(finalize_module, "build_card", lambda _root: actions.append("card"))

    finalize_module._advance_snapshot_state(tmp_path, state)

    assert transitions == [
        STATUS_EXTRACTED,
        STATUS_ENRICHING,
        STATUS_ENRICHED,
        STATUS_ANALYZED,
        STATUS_CARD_BUILT,
    ]
    assert actions == ["analyze", "card"]


def test_finalize_run_requires_card_built_or_complete_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification = type("Report", (), {"ok": True, "checked_shards": ("a",)})()
    state = type("State", (), {"metadata": {"status": STATUS_CARD_BUILT}})()
    transitions: list[str] = []
    monkeypatch.setattr(finalize_module, "verify_results", lambda _root: verification)
    monkeypatch.setattr(finalize_module, "load_run", lambda _root: state)
    monkeypatch.setattr(
        finalize_module,
        "transition_status",
        lambda _state, status: transitions.append(status),
    )
    monkeypatch.setattr(
        finalize_module,
        "_write_completion_receipt",
        lambda _root: {"manifest_digest": "a" * 64},
    )

    result = finalize_run(tmp_path)

    assert result.ok is True
    assert result.receipt == {"manifest_digest": "a" * 64}
    assert result.verification is verification
    assert transitions == [STATUS_VERIFIED, STATUS_COMPLETE]

    state.metadata["status"] = STATUS_ANALYZED
    failed = finalize_run(tmp_path)
    assert failed.ok is False
    assert failed.receipt == {}
    assert failed.verification.ok is False
    assert failed.verification.checked_shards == ("a",)
    assert "card_built or complete" in failed.verification.errors[0]


def test_replace_receipt_atomic_delegates_to_completion_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"manifest_digest": "b" * 64}
    monkeypatch.setattr(finalize_module, "_write_completion_receipt", lambda root: expected)

    assert replace_receipt_atomic(tmp_path) is expected
