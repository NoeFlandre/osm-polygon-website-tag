"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.application.workflow import (
    discover_sources,
    run_all,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.contracts.text_schema import count_words
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
from osm_polygon_website_tag.reporting.finalize import FinalizationReport
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.verify import VerificationReport
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_EXTRACTING,
    hash_shard,
    load_run,
    upsert_run_metadata,
)
from osm_polygon_website_tag.web.text_extract import TextExtraction
from osm_polygon_website_tag.web.web_fetch import FetchResult

_EMPTY_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/></osm>
"""


_WEBSITE_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/><tag k="contact:website" v="example.org"/>
  </way>
</osm>
"""


def _noop_progress(_message: str) -> None:
    return None


class RecordingLanguageDetector:
    """Small deterministic detector for workflow tests."""

    identity = ModelIdentity("repo", "model.bin", "revision", "a" * 64)

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        self.calls.append(list(texts))
        return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]


class InterruptingLanguageDetector(RecordingLanguageDetector):
    """Detector that interrupts after a selected prediction call."""

    def __init__(self, *, interrupt_on_call: int) -> None:
        super().__init__()
        self.interrupt_on_call = interrupt_on_call

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        result = super().predict(texts)
        if len(self.calls) == self.interrupt_on_call:
            raise KeyboardInterrupt
        return result


@pytest.fixture(autouse=True)
def _offline_remote_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep workflow tests local; remote reconciliation has dedicated unit tests."""
    from osm_polygon_website_tag.publishing.incremental import load_upload_checkpoint

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.reconcile_upload_checkpoint",
        lambda run_dir, **_kwargs: load_upload_checkpoint(run_dir),
    )


def _write_card_contract_fixture(run_dir: Path, receipt: object) -> None:
    map_path = run_dir / POLYGON_DENSITY_ASSET_REL_PATH
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"map")
    (run_dir / "stats.json").write_text("stats")
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt))


def _checkpoint() -> CheckpointV2:
    return {"schema_version": "v2", "global_bundle": {}, "sources": {}}


def _sources(make_pbf, tmp_path: Path) -> Path:
    first = make_pbf(_WEBSITE_OSM, name="a-latest.osm.pbf")
    second = make_pbf(_EMPTY_OSM, name="b-latest.osm.pbf")
    root = tmp_path / "sources"
    root.mkdir()
    (root / "a-latest.osm.pbf").write_bytes((first / "a-latest.osm.pbf").read_bytes())
    nested = root / "nested"
    nested.mkdir()
    (nested / "b-latest.osm.pbf").write_bytes((second / "b-latest.osm.pbf").read_bytes())
    return root


@pytest.fixture(autouse=True)
def _inject_static_text_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    from osm_polygon_website_tag.pipeline.enrich import enrich_polygon_shard as real_enrich

    def enrich(shard, **kwargs):
        return real_enrich(
            shard,
            **kwargs,
            fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"website text"),
            extractor=lambda _html, *, url: TextExtraction(
                "success",
                f"text from {url}",
                count_words(f"text from {url}"),
                None,
                "2.1.0",
            ),
        )

    monkeypatch.setattr(source_processing, "enrich_polygon_shard", enrich, raising=False)


def test_finalize_forwards_progress_and_requires_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress: list[str] = []
    calls: list[Path] = []
    context = cast(
        Any,
        type("Context", (), {"run_dir": tmp_path, "progress": progress.append})(),
    )
    monkeypatch.setattr(
        workflow,
        "finalize_run",
        lambda run_dir: (
            calls.append(run_dir) or FinalizationReport(True, {}, VerificationReport(True))
        ),
    )

    assert workflow._finalize_if_needed(STATUS_CARD_BUILT, context) == STATUS_COMPLETE
    assert calls == [tmp_path]
    assert progress == ["Verifying and finalizing the complete run"]


def test_publish_complete_run_forwards_receipt_upload_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress: list[str] = []
    calls: list[tuple[Path, str, bool]] = []
    context = cast(
        Any,
        type(
            "Context",
            (),
            {
                "run_dir": tmp_path,
                "repo_id": "owner/dataset",
                "apply": True,
                "progress": progress.append,
            },
        )(),
    )
    monkeypatch.setattr(
        workflow,
        "publish_to_hf",
        lambda run_dir, *, repo_id, dry_run: calls.append((run_dir, repo_id, dry_run)),
    )

    workflow._publish_complete_run(STATUS_COMPLETE, context)

    assert calls == [(tmp_path, "owner/dataset", False)]
    assert progress == ["Uploading the receipt-bound complete dataset"]


def test_discover_sources_is_recursive_sorted_and_rejects_duplicate_names(
    make_pbf,
    tmp_path: Path,
) -> None:
    root = _sources(make_pbf, tmp_path)
    assert [path.name for path in discover_sources(root)] == [
        "a-latest.osm.pbf",
        "b-latest.osm.pbf",
    ]
    duplicate = root / "nested"
    (duplicate / "a-latest.osm.pbf").write_bytes(b"not read")
    with pytest.raises(ValueError, match="duplicate source filenames"):
        discover_sources(root)


def test_run_all_dry_run_completes_without_remote_calls(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing._upload_public_shard",
        lambda *_args: pytest.fail("dry-run must not upload"),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.publish_to_hf",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not publish"),
    )

    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
    )

    assert result.complete
    assert result.extracted_count == 2
    assert result.uploaded_count == 0
    assert load_run(result.run_dir).metadata["status"] == STATUS_COMPLETE


def test_run_all_default_does_not_load_language_model(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    monkeypatch.setattr(
        workflow,
        "load_glotlid_detector",
        lambda *_args, **_kwargs: pytest.fail("default run-all must not load GlotLID"),
        raising=False,
    )

    result = run_all(
        source_root=_sources(make_pbf, tmp_path),
        output_root=tmp_path / "runs",
        run_id="plain",
    )

    assert result.complete
    assert all(
        pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
        for path in (result.run_dir / "polygons").glob("*.parquet")
    )


def test_run_all_opt_in_detects_and_publishes_language_shards(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    detector = RecordingLanguageDetector()
    uploads: list[str] = []
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda _run, source, _repo, *_args: uploads.append(source.name),
    )
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    result = run_all(
        source_root=_sources(make_pbf, tmp_path),
        output_root=tmp_path / "runs",
        run_id="language",
        apply=True,
        detect_languages=True,
        language_detector=detector,
    )

    assert result.complete
    assert detector.calls == [["text from https://example.org"]]
    assert uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    for path in (result.run_dir / "polygons").glob("*.parquet"):
        assert pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
        assert load_run(result.run_dir).sources[path.stem + ".osm.pbf"]["public_shard_sha256"] == (
            hash_shard(path)
        )


def test_run_all_opt_in_does_not_downgrade_existing_v1_4_shards(
    make_pbf,
    tmp_path: Path,
) -> None:
    root = _sources(make_pbf, tmp_path)
    first = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="language",
        detect_languages=True,
        language_detector=RecordingLanguageDetector(),
    )
    detector = RecordingLanguageDetector()

    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="language",
        detect_languages=True,
        language_detector=detector,
    )

    assert resumed.complete
    assert detector.calls == []
    assert all(
        pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
        for path in (first.run_dir / "polygons").glob("*.parquet")
    )


def test_run_all_language_detection_resumes_after_an_interrupted_shard(
    make_pbf,
    tmp_path: Path,
) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    for name in ("a-latest.osm.pbf", "b-latest.osm.pbf"):
        generated = make_pbf(_WEBSITE_OSM, name=name)
        (root / name).write_bytes((generated / name).read_bytes())

    with pytest.raises(KeyboardInterrupt):
        run_all(
            source_root=root,
            output_root=tmp_path / "runs",
            run_id="language",
            detect_languages=True,
            language_detector=InterruptingLanguageDetector(interrupt_on_call=2),
        )

    resumed_detector = RecordingLanguageDetector()
    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="language",
        detect_languages=True,
        language_detector=resumed_detector,
    )

    assert result.complete
    assert resumed_detector.calls == [["text from https://example.org"]]
    assert all(
        pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
        for path in (result.run_dir / "polygons").glob("*.parquet")
    )


def test_run_all_refreshes_legacy_complete_card_without_reprocessing_sources(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed pre-map run is upgraded locally on the next resume."""
    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    map_path = first.run_dir / "assets" / "geographic_polygon_density.png"
    map_path.unlink()
    receipt_path = first.run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("card_contract_version", None)
    receipt_path.write_text(json.dumps(receipt))

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing.extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("legacy card refresh must not read PBFs"),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing.enrich_polygon_shard",
        lambda *_args, **_kwargs: pytest.fail("legacy card refresh must not fetch websites"),
    )

    resumed = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert resumed.extracted_count == 0
    assert map_path.is_file()
    assert json.loads(receipt_path.read_text())["card_contract_version"] == 2


def test_run_all_does_not_resume_a_finalized_frozen_snapshot(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalized snapshot is immutable and never retries website failures."""
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    state = load_run(first.run_dir)
    upsert_run_metadata(state, {"snapshot_status": "done"})

    monkeypatch.setattr(
        workflow,
        "discover_sources",
        lambda _root: pytest.fail("frozen resume must not rediscover source PBFs"),
    )
    monkeypatch.setattr(
        source_processing,
        "enrich_polygon_shard",
        lambda *_args, **_kwargs: pytest.fail("frozen resume must not retry websites"),
    )
    progress: list[str] = []
    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        progress=progress.append,
    )

    assert resumed.complete
    assert resumed.source_count == 2
    assert resumed.extracted_count == 0
    assert resumed.skipped_count == 0
    assert resumed.uploaded_count == 0
    assert resumed.dry_run is True
    assert load_run(first.run_dir).metadata["snapshot_status"] == "done"
    assert progress == ["Frozen snapshot is already complete; skipping enrichment and uploads"]

    monkeypatch.setattr(
        workflow,
        "resolve_hf_token",
        lambda: pytest.fail("frozen resume must not resolve upload credentials"),
    )
    applied_resume = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    assert applied_resume.complete
    assert applied_resume.dry_run is False
    assert applied_resume.extracted_count == 0
    assert applied_resume.uploaded_count == 0


def test_run_all_resumes_after_ctrl_c(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    original = source_processing.extract_pbf
    calls = 0

    def interrupt_second(source: Path, run_dir: Path, run_state=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(source, run_dir, run_state=run_state)

    monkeypatch.setattr(source_processing, "extract_pbf", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    run_dir = tmp_path / "runs" / "production"
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING

    monkeypatch.setattr(source_processing, "extract_pbf", original)
    result = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert result.complete
    assert result.skipped_count == 1
    assert result.extracted_count == 1
