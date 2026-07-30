"""Tests for the resumable end-to-end workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.application.workflow import (
    _upload_public_shard,
    discover_sources,
    run_all,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
)
from osm_polygon_website_tag.contracts.text_schema import count_words
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_COMPLETE,
    STATUS_ENRICHING,
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
    from osm_polygon_website_tag.application import workflow
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

    monkeypatch.setattr(workflow, "enrich_polygon_shard", enrich, raising=False)


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
        "osm_polygon_website_tag.application.workflow._upload_public_shard",
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


def test_run_all_resumes_after_ctrl_c(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

    root = _sources(make_pbf, tmp_path)
    original = workflow.extract_pbf
    calls = 0

    def interrupt_second(source: Path, run_dir: Path, run_state=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(source, run_dir, run_state=run_state)

    monkeypatch.setattr(workflow, "extract_pbf", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    run_dir = tmp_path / "runs" / "production"
    assert load_run(run_dir).metadata["status"] == STATUS_EXTRACTING

    monkeypatch.setattr(workflow, "extract_pbf", original)
    result = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert result.complete
    assert result.skipped_count == 1
    assert result.extracted_count == 1


def test_run_all_apply_uploads_each_shard_then_complete_run(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    shard_uploads: list[str] = []
    final_uploads: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.resolve_hf_token", lambda: "available"
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow._upload_public_shard",
        lambda _run, source, _repo: shard_uploads.append(source.name),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.publish_to_hf",
        lambda run_dir, **_kwargs: final_uploads.append(Path(run_dir)),
    )

    result = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    assert final_uploads == [result.run_dir]
    assert result.uploaded_count == 2


def test_run_all_refuses_changed_source_inventory(make_pbf, tmp_path: Path) -> None:
    root = _sources(make_pbf, tmp_path)
    result = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    source = next(root.rglob("a-latest.osm.pbf"))
    source.touch()

    with pytest.raises(ValueError, match="inventory changed"):
        run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert result.complete


def test_complete_legacy_run_migrates_without_reextracting_pbf(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sources(make_pbf, tmp_path)
    first = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")
    shard = first.run_dir / "polygons" / "a-latest.parquet"
    current = pq.read_table(shard)
    legacy = current.select(POLYGON_PUBLIC_SCHEMA_V1_1.names).cast(POLYGON_PUBLIC_SCHEMA_V1_1)
    pq.write_table(legacy, shard)
    state = load_run(first.run_dir)
    update_public_shard_metadata(
        state,
        filename="a-latest.osm.pbf",
        row_count=legacy.num_rows,
        shard_sha256=hash_shard(shard),
    )
    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("legacy migration must not read PBF"),
    )

    resumed = run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert resumed.extracted_count == 0
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert load_run(first.run_dir).metadata["status"] == STATUS_COMPLETE


def test_incremental_upload_includes_shard_and_recomputed_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    shard = run_dir / "polygons" / "source.parquet"
    shard.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), shard)
    (run_dir / "README.md").write_text("card")
    (run_dir / "dataset.yaml").write_text("metadata")
    captured: list[Path] = []

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    _upload_public_shard(run_dir, Path("source.osm.pbf"), "owner/dataset")

    assert captured == [shard, run_dir / "README.md", run_dir / "dataset.yaml"]


def test_resume_enriches_only_shards_with_retryable_text(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_website_tag.application import workflow

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
    original = workflow.enrich_polygon_shard
    enriched: list[str] = []

    def track(shard, **kwargs):
        enriched.append(Path(shard).name)
        return original(shard, **kwargs)

    monkeypatch.setattr(workflow, "enrich_polygon_shard", track)

    run_all(source_root=root, output_root=tmp_path / "runs", run_id="production")

    assert enriched == ["a-latest.parquet"]


def test_run_all_apply_resume_after_keyboard_interrupt_preserves_checkpoint(
    make_pbf,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume from STATUS_ENRICHING after a mid-loop KeyboardInterrupt.

    This characterization test directly exercises the per-shard upload
    checkpoint branch by interrupting the second incremental upload
    and then resuming with ``apply=True``. It protects:

    * checkpoint persistence only after a successful upload,
    * resumption from ``STATUS_ENRICHING`` (no transition to ENRICHED
      when interrupted),
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

    def upload_shard(_run_dir, source, _repo_id):
        shard_uploads.append(source.name)
        # Raise KeyboardInterrupt only once: on the first attempt to upload
        # source "b". The resume invocation should complete normally.
        if source.name == "b-latest.osm.pbf" and not interrupted["done"]:
            interrupted["done"] = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow._upload_public_shard", upload_shard
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

    # Resumable status: the loop was interrupted before the ENRICHED
    # transition could run.
    assert load_run(run_dir).metadata["status"] == STATUS_ENRICHING

    # Checkpoint persisted only for the first, successful upload.
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    assert checkpoint_path.is_file()
    checkpoint = json.loads(checkpoint_path.read_text())
    assert set(checkpoint.keys()) == {"a-latest.osm.pbf"}

    # No final publication during the interrupted invocation.
    assert final_uploads == []
    # Both shards reached the upload attempt; the second one raised.
    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf"]
    pre_resume_checkpoint = checkpoint_path.read_text()

    # Resume the same run.
    resumed = run_all(
        source_root=root,
        output_root=tmp_path / "runs",
        run_id="production",
        apply=True,
    )

    # Only the second shard is uploaded during this invocation.
    assert shard_uploads == ["a-latest.osm.pbf", "b-latest.osm.pbf", "b-latest.osm.pbf"]
    assert resumed.uploaded_count == 1

    # The checkpoint now covers both sources, and the entry for the
    # already-acknowledged first source is unchanged (byte-identical
    # checkpoint file except for the addition of the second entry).
    final_checkpoint = json.loads(checkpoint_path.read_text())
    assert set(final_checkpoint.keys()) == {"a-latest.osm.pbf", "b-latest.osm.pbf"}
    # The first source's entry survived intact.
    parsed_pre_resume = json.loads(pre_resume_checkpoint)
    assert final_checkpoint["a-latest.osm.pbf"] == parsed_pre_resume["a-latest.osm.pbf"]

    # Final publication happened exactly once, only after successful
    # completion.
    assert final_uploads == [run_dir]
    assert load_run(run_dir).metadata["status"] == STATUS_COMPLETE
