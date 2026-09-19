"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.application import source_processing, workflow
from osm_polygon_website_tag.application.workflow import (
    run_all,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
)
from osm_polygon_website_tag.contracts.text_schema import count_words
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_COMPLETE,
    STATUS_EXTRACTING,
    hash_shard,
    load_run,
    update_public_shard_metadata,
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


def test_incremental_upload_includes_recomputed_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    shard = run_dir / "polygons" / "source.parquet"
    shard.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), shard)
    (run_dir / "README.md").write_text("card")
    (run_dir / "dataset.yaml").write_text("metadata")
    (run_dir / "stats.json").write_text("stats")
    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir()
    map_path.write_bytes(b"map")
    captured: list[Path] = []

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    source_processing._upload_public_shard(run_dir, Path("source.osm.pbf"), "owner/dataset")

    assert captured == [
        shard,
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "stats.json",
        map_path,
    ]


def test_resume_enriches_only_shards_with_retryable_text(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    retry_shard = first.run_dir / "polygons" / "a-latest.parquet"
    rows = pq.read_table(retry_shard).to_pylist()
    rows[0]["contact_website_text"] = None
    rows[0]["contact_website_word_count"] = None
    rows[0]["contact_website_text_status"] = "fetch_error"
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), retry_shard)
    state = load_run(first.run_dir)
    update_public_shard_metadata(
        state,
        filename="a-latest.osm.pbf",
        row_count=len(rows),
        shard_sha256=hash_shard(retry_shard),
    )
    original = source_processing.enrich_polygon_shard
    enriched: list[str] = []
    bundle_checks: list[str] = []

    original_bundle_check = source_processing.source_bundle_is_complete

    def track_bundle_check(run_dir, manifest, fingerprint):
        bundle_checks.append(fingerprint.filename)
        return original_bundle_check(run_dir, manifest, fingerprint)

    monkeypatch.setattr(source_processing, "source_bundle_is_complete", track_bundle_check)

    def track(shard, **kwargs):
        enriched.append(Path(shard).name)
        return original(shard, **kwargs)

    monkeypatch.setattr(source_processing, "enrich_polygon_shard", track)

    run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert enriched == ["a-latest.parquet"]
    assert bundle_checks == ["a-latest.osm.pbf", "b-latest.osm.pbf"]


def test_run_all_apply_resume_after_keyboard_interrupt_preserves_checkpoint(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume an interleaved extraction after a mid-upload KeyboardInterrupt.

    This characterization test directly exercises the per-shard upload
    checkpoint branch by interrupting the second incremental upload
    and then resuming with ``apply=True``. It protects:

    * checkpoint persistence only after a successful upload,
    * resumption from ``STATUS_EXTRACTING`` while the inventory is incomplete,
    * skipping the already-acknowledged first shard on resume,
    * retrying the interrupted second shard on resume,
    * ``uploaded_count`` counting only the upload performed during
      that invocation,
    * final publication occurring only after successful completion,
    * ``KeyboardInterrupt`` propagation (not swallowed).
    """
    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.resolve_hf_token", lambda: "available"
    )

    shard_uploads: list[str] = []
    final_uploads: list[Path] = []

    interrupted = {"done": False}

    def upload_shard(_run_dir, source, _repo_id, *_args):
        shard_uploads.append(source.name)
        # Raise KeyboardInterrupt only once: on the first attempt to upload
        # source "b". The resume invocation should complete normally.
        if source.name == "b-latest.osm.pbf" and not interrupted["done"]:
            interrupted["done"] = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.source_processing._upload_public_shard", upload_shard
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.publish_to_hf",
        lambda run_dir, **_kwargs: final_uploads.append(Path(run_dir)),
    )

    with pytest.raises(KeyboardInterrupt):
        run_all(
            source_root=root,
            output_root=tmp_path / "runs",
            run_id="production",
            apply=True,
        )

    run_dir = tmp_path / "runs" / "production"

    # The inventory-level extraction state remains active until every
    # per-source transaction has succeeded.
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING

    # Checkpoint persisted only for the first, successful upload.
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    assert checkpoint_path.is_file()
    checkpoint = json.loads(checkpoint_path.read_text())
    assert set(checkpoint) == {"schema_version", "global_bundle", "sources"}
    assert set(checkpoint["sources"]) == {"a-latest.osm.pbf"}

    # No final publication during the interrupted invocation.
    assert final_uploads == []
    # Both shards reached the upload attempt; the second one raised.
    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    pre_resume_checkpoint = checkpoint_path.read_text()

    # Resume the same run.
    resumed_source_calls: list[str] = []
    original_publish = source_processing._maybe_publish_enriched_shard

    def track_resume_publish(**kwargs):
        resumed_source_calls.append(Path(kwargs["source"]).name)
        return original_publish(**kwargs)

    monkeypatch.setattr(source_processing, "_maybe_publish_enriched_shard", track_resume_publish)
    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    # Only the second shard is uploaded during this invocation.
    assert resumed_source_calls == ["b-latest.osm.pbf"]
    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf", "b-latest.osm.pbf"]
    assert resumed.uploaded_count == 1

    # The checkpoint now covers both sources, and the entry for the
    # already-acknowledged first source is unchanged (byte-identical
    # checkpoint file except for the addition of the second entry).
    final_checkpoint = json.loads(checkpoint_path.read_text())
    assert set(final_checkpoint["sources"]) == {"a-latest.osm.pbf", "b-latest.osm.pbf"}
    # The first source's entry survived intact.
    parsed_pre_resume = json.loads(pre_resume_checkpoint)
    assert (
        final_checkpoint["sources"]["a-latest.osm.pbf"]
        == parsed_pre_resume["sources"]["a-latest.osm.pbf"]
    )

    # Final publication happened exactly once, only after successful
    # completion.
    assert final_uploads == [run_dir]
    assert load_run(run_dir).metadata["status"] == STATUS_COMPLETE


def test_workflow_resume_after_acknowledged_shard_is_skipped(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shard already present in ``uploaded_polygons.json`` with the current
    public shard SHA-256 is skipped on resume: no upload call is made and the
    checkpoint entry is left byte-identical."""

    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(source_processing, "_upload_public_shard", lambda *_args: None)
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    first = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    checkpoint_path = first.run_dir / "manifests" / "uploaded_polygons.json"
    pre_resume = checkpoint_path.read_text()
    uploaded_during_resume: list[str] = []

    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda _run, source, _repo, *_args: uploaded_during_resume.append(source.name),
    )

    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    assert uploaded_during_resume == []
    assert resumed.uploaded_count == 0
    assert checkpoint_path.read_text() == pre_resume


def test_workflow_upload_checkpoint_persistence_is_deterministic(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-uploading the same shard on a fresh apply-mode invocation rewrites
    the per-shard checkpoint entry to a deterministic value: identical key
    set, source ordering, and JSON formatting."""

    root = _sources(make_pbf, tmp_path)
    monkeypatch.setattr(workflow, "resolve_hf_token", lambda: "available")
    monkeypatch.setattr(source_processing, "_upload_public_shard", lambda *_args: None)
    monkeypatch.setattr(workflow, "publish_to_hf", lambda *_args, **_kwargs: None)

    first = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    checkpoint_path = first.run_dir / "manifests" / "uploaded_polygons.json"
    first_bytes = checkpoint_path.read_text()

    second = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )
    second_bytes = checkpoint_path.read_text()

    assert first.run_dir == second.run_dir
    assert first_bytes == second_bytes
    parsed = json.loads(first_bytes)
    assert parsed["schema_version"] == "v2"
    assert set(parsed["sources"]) == {"a-latest.osm.pbf", "b-latest.osm.pbf"}
    assert set(parsed) == {"schema_version", "global_bundle", "sources"}
