"""Tests for the offline Grid'5000 language-detection bundle boundary."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import legacy_polygon_row

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields
from osm_polygon_website_tag.pipeline import grid5000
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint
from osm_polygon_website_tag.pipeline.detect_languages import (
    LanguageDetectionResult,
    detect_language_shard,
)
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    RunState,
    atomic_write_json,
    hash_shard,
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
    transition_status,
)


def _write_enriched_run(tmp_path: Path, *, row_count: int = 1) -> Path:
    run_dir, state = initialise_run(tmp_path / "runs", run_id="run")
    source = tmp_path / "source.osm.pbf"
    source.write_bytes(b"source")
    rows: list[dict[str, object]] = []
    for index in range(row_count):
        row = legacy_polygon_row(polygon_id=f"source:way/{index + 1}", contact=None)
        for name in (
            "preferred_website",
            "preferred_website_source",
            "wikidata",
            "wikidata_qid",
            "wikidata_class",
            "area_km2",
        ):
            row.pop(name)
        row.update(
            initial_text_fields(website_present=True, contact_website_present=False),
            website_text=f"English text {index}",
            website_word_count=2,
            website_text_status="success",
            contact_website_text=None,
            contact_website_word_count=None,
            contact_website_text_status="absent",
            schema_version="v1.3",
        )
        rows.append(row)
    shard = run_dir / "polygons" / "source.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), shard)
    record_processed_source(
        state,
        snapshot_source_fingerprint(source),
        public_row_count=row_count,
        public_shard_sha256=hash_shard(shard),
    )
    transition_status(state, "extracting")
    transition_status(state, "extracted")
    transition_status(state, "enriching")
    transition_status(state, "enriched")
    return run_dir


def test_prepare_bundle_records_source_and_model_identity(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")

    manifest = grid5000.prepare_language_bundle(
        run_dir,
        tmp_path / "bundle",
        model_path=model,
        commit="abc123",
    )

    assert manifest.source_shard == "source.parquet"
    assert manifest.source_row_count == 1
    assert manifest.source_shard_sha256 == hash_shard(run_dir / "polygons" / "source.parquet")
    assert manifest.model.sha256 == hashlib.sha256(b"model").hexdigest()
    assert manifest.commit == "abc123"
    assert (tmp_path / "bundle" / "model_v3.bin").read_bytes() == b"model"
    assert (tmp_path / "bundle" / "source.parquet").read_bytes() == (
        run_dir / "polygons" / "source.parquet"
    ).read_bytes()


def test_prepare_bundle_reuses_model_storage_on_the_same_filesystem(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")

    grid5000.prepare_language_bundle(
        run_dir,
        tmp_path / "bundle",
        model_path=model,
        commit="abc123",
    )

    assert (tmp_path / "bundle" / "model_v3.bin").samefile(model)


def test_run_bundle_uses_only_the_staged_model_and_writes_a_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir,
        bundle_dir,
        model_path=model,
        commit="abc123",
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())

    result = grid5000.run_language_bundle(bundle_dir, job_id="42")

    assert result.completed is True
    assert result.changed is True
    assert result.job_id == "42"
    assert result.shard_sha256 == hash_shard(bundle_dir / "source.parquet")
    assert pq.read_schema(bundle_dir / "source.parquet").equals(
        POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True
    )
    assert (bundle_dir / grid5000.RESULT_NAME).is_file()


def test_sync_paused_bundle_preserves_source_and_installs_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = _write_enriched_run(tmp_path, row_count=2)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir,
        bundle_dir,
        model_path=model,
        commit="abc123",
        batch_rows=1,
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    clock_values = iter([0.0, 0.5, 1.1])
    grid5000.run_language_bundle(
        bundle_dir,
        time_budget_seconds=1,
        job_id="42",
        clock=lambda: next(clock_values),
    )

    original = (run_dir / "polygons" / "source.parquet").read_bytes()
    local_checkpoint = run_dir / "polygons" / ".source.parquet.language.parts"
    local_checkpoint.mkdir()
    (local_checkpoint / "old").write_text("old", encoding="utf-8")
    result = grid5000.sync_language_bundle(bundle_dir, run_dir)

    assert result.completed is False
    assert (run_dir / "polygons" / "source.parquet").read_bytes() == original
    assert (
        run_dir / "polygons" / ".source.parquet.language.parts" / "part-00000000.parquet"
    ).is_file()
    assert not list(local_checkpoint.parent.glob(f".{local_checkpoint.name}.*.backup"))
    history = sorted((run_dir / "manifests" / "grid5000").glob("*.json"))
    assert len(history) == 1
    assert json.loads(history[0].read_text(encoding="utf-8"))["action"] == "paused"


def test_sync_completed_bundle_promotes_shard_and_updates_run_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir,
        bundle_dir,
        model_path=model,
        commit="abc123",
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    grid5000.run_language_bundle(bundle_dir, job_id="42")

    local_checkpoint = run_dir / "polygons" / ".source.parquet.language.parts"
    local_checkpoint.mkdir()
    (local_checkpoint / "old").write_text("old", encoding="utf-8")
    result = grid5000.sync_language_bundle(bundle_dir, run_dir)

    assert result.completed is True
    assert not local_checkpoint.exists()
    assert pq.read_schema(run_dir / "polygons" / "source.parquet").equals(
        POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True
    )
    assert load_run(run_dir).metadata["status"] == "enriched"
    source_entry = load_run(run_dir).sources["source.osm.pbf"]
    assert source_entry["public_row_count"] == result.source_row_count
    assert source_entry["public_shard_sha256"] == result.shard_sha256
    history = sorted((run_dir / "manifests" / "grid5000").glob("*.json"))
    assert len(history) == 1
    payload = json.loads(history[0].read_text(encoding="utf-8"))
    expected_digest = hashlib.sha256(
        json.dumps(result.payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    assert history[0].name == f"source-{expected_digest}.json"
    assert payload == {
        "action": "completed",
        "bundle": bundle.payload(),
        "result": result.payload(),
    }

    fresh_history_root = tmp_path / "fresh-history"
    grid5000._write_sync_history(fresh_history_root, bundle, result)
    assert list((fresh_history_root / "manifests" / "grid5000").glob("*.json"))


def test_sync_completed_bundle_finishes_only_an_enriching_run_when_all_shards_are_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    grid5000.run_language_bundle(bundle_dir)
    state = load_run(run_dir)
    state.metadata["status"] = STATUS_ENRICHING
    atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)
    grid5000.sync_language_bundle(bundle_dir, run_dir)

    assert load_run(run_dir).metadata["status"] == STATUS_ENRICHED


def test_sync_completed_bundle_does_not_finish_with_an_incomplete_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    grid5000.run_language_bundle(bundle_dir)
    state = load_run(run_dir)
    state.metadata["status"] = STATUS_ENRICHING
    atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)
    monkeypatch.setattr(grid5000, "_all_language_shards_complete", lambda _path: False)
    grid5000.sync_language_bundle(bundle_dir, run_dir)

    assert load_run(run_dir).metadata["status"] == STATUS_ENRICHING


def test_all_language_shards_complete_requires_a_nonempty_complete_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    (empty / "polygons").mkdir(parents=True)
    assert grid5000._all_language_shards_complete(empty) is False

    run_dir = _write_enriched_run(tmp_path / "run")
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    grid5000.run_language_bundle(bundle_dir)

    assert grid5000._all_language_shards_complete(run_dir) is False
    grid5000.sync_language_bundle(bundle_dir, run_dir)
    assert grid5000._all_language_shards_complete(run_dir) is True


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True])
def test_prepare_rejects_invalid_grid_time_budgets(tmp_path: Path, budget: object) -> None:
    with pytest.raises(ValueError, match=r"^time_budget_seconds must be positive$"):
        grid5000.prepare_language_bundle(
            tmp_path / "missing-run",
            tmp_path / "bundle",
            model_path=tmp_path / "model.bin",
            commit="abc123",
            time_budget_seconds=cast(int, budget),
        )


def test_run_rejects_a_model_changed_after_preparation(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    grid5000.prepare_language_bundle(run_dir, bundle_dir, model_path=model, commit="abc123")
    (bundle_dir / "model_v3.bin").write_bytes(b"tampered")

    with pytest.raises(ValueError, match=r"^staged model identity does not match bundle$"):
        grid5000.run_language_bundle(bundle_dir)


def test_run_reuses_a_valid_receipt_without_loading_the_model(tmp_path: Path, monkeypatch) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )
    receipt = {
        "changed": False,
        "commit": bundle.commit,
        "completed": False,
        "job_id": "42",
        "max_batch_rows": 0,
        "model": {
            "filename": bundle.model.filename,
            "repository": bundle.model.repository,
            "revision": bundle.model.revision,
            "sha256": bundle.model.sha256,
        },
        "processed_rows": 0,
        "run_id": bundle.run_id,
        "schema_version": bundle.schema_version,
        "source_row_count": bundle.source_row_count,
        "source_shard": bundle.source_shard,
        "shard_sha256": bundle.source_shard_sha256,
    }
    (bundle_dir / grid5000.RESULT_NAME).write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(
        grid5000,
        "load_glotlid_detector_from_path",
        lambda _path: (_ for _ in ()).throw(AssertionError("model reloaded")),
    )

    assert grid5000.run_language_bundle(bundle_dir) == grid5000.Grid5000Result(
        run_id=bundle.run_id,
        source_shard=bundle.source_shard,
        source_row_count=bundle.source_row_count,
        shard_sha256=bundle.source_shard_sha256,
        model=bundle.model,
        commit=bundle.commit,
        completed=False,
        changed=False,
        processed_rows=0,
        max_batch_rows=0,
        job_id="42",
    )


def test_run_revalidates_staged_model_when_reusing_a_receipt(tmp_path: Path, monkeypatch) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    grid5000.run_language_bundle(bundle_dir)
    (bundle_dir / bundle.model.filename).write_bytes(b"tampered")

    with pytest.raises(ValueError, match=r"^staged model identity does not match bundle$"):
        grid5000.run_language_bundle(bundle_dir)


def test_run_revalidates_completed_receipt_before_reuse(tmp_path: Path, monkeypatch) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    expected = grid5000.run_language_bundle(bundle_dir)
    monkeypatch.setattr(
        grid5000,
        "load_glotlid_detector_from_path",
        lambda _path: (_ for _ in ()).throw(AssertionError("model reloaded")),
    )

    assert grid5000.run_language_bundle(bundle_dir) == expected


def test_run_revalidates_paused_receipt_before_reuse(tmp_path: Path, monkeypatch) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )
    receipt = {
        "changed": False,
        "commit": bundle.commit,
        "completed": False,
        "job_id": "42",
        "max_batch_rows": 0,
        "model": {
            "filename": bundle.model.filename,
            "repository": bundle.model.repository,
            "revision": bundle.model.revision,
            "sha256": bundle.model.sha256,
        },
        "processed_rows": 0,
        "run_id": bundle.run_id,
        "schema_version": bundle.schema_version,
        "source_row_count": bundle.source_row_count,
        "source_shard": bundle.source_shard,
        "shard_sha256": bundle.source_shard_sha256,
    }
    (bundle_dir / grid5000.RESULT_NAME).write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(
        grid5000,
        "load_glotlid_detector_from_path",
        lambda _path: (_ for _ in ()).throw(AssertionError("model reloaded")),
    )

    assert grid5000.run_language_bundle(bundle_dir) == grid5000.Grid5000Result(
        run_id=bundle.run_id,
        source_shard=bundle.source_shard,
        source_row_count=bundle.source_row_count,
        shard_sha256=bundle.source_shard_sha256,
        model=bundle.model,
        commit=bundle.commit,
        completed=False,
        changed=False,
        processed_rows=0,
        max_batch_rows=0,
        job_id="42",
    )


def test_run_passes_the_staged_model_path_to_the_loader(tmp_path: Path, monkeypatch) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )
    observed: list[Path] = []

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(
        grid5000,
        "load_glotlid_detector_from_path",
        lambda path: observed.append(Path(path)) or FakeDetector(),
    )
    grid5000.run_language_bundle(bundle_dir)

    assert observed == [bundle_dir / bundle.model.filename]


def test_result_from_detection_rejects_an_empty_job_id(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle = grid5000.prepare_language_bundle(
        run_dir, tmp_path / "bundle", model_path=model, commit="abc123"
    )
    detection = LanguageDetectionResult(
        shard_path=tmp_path / "source.parquet",
        row_count=bundle.source_row_count,
        changed=False,
        shard_sha256=bundle.source_shard_sha256,
        max_batch_rows=0,
        processed_rows=0,
    )

    with pytest.raises(ValueError, match=r"^job_id must be null or a non-empty string$"):
        grid5000._result_from_detection(bundle, detection, job_id="")


def test_prepare_copies_a_validated_checkpoint_prefix(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path, row_count=2)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    model_identity = grid5000.model_identity_for_path(model)

    class FakeDetector:
        identity = model_identity

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    clock_values = iter([0.0, 0.5, 1.1])
    detect_language_shard(
        run_dir / "polygons" / "source.parquet",
        detector=FakeDetector(),
        batch_rows=1,
        time_budget_seconds=1,
        clock=lambda: next(clock_values),
    )
    source_checkpoint = run_dir / "polygons" / ".source.parquet.language.parts"

    grid5000.prepare_language_bundle(
        run_dir,
        tmp_path / "bundle",
        model_path=model,
        commit="abc123",
        batch_rows=1,
    )

    staged_checkpoint = tmp_path / "bundle" / source_checkpoint.name
    assert (staged_checkpoint / "checkpoint.json").read_bytes() == (
        source_checkpoint / "checkpoint.json"
    ).read_bytes()
    assert (staged_checkpoint / "part-00000000.parquet").read_bytes() == (
        source_checkpoint / "part-00000000.parquet"
    ).read_bytes()


def test_copy_checkpoint_allows_a_prefix_at_the_source_row_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle = grid5000.prepare_language_bundle(
        run_dir, tmp_path / "bundle", model_path=model, commit="abc123"
    )
    source = tmp_path / "bundle" / bundle.source_shard
    source_checkpoint = source.with_name(f".{source.name}.language.parts")
    source_checkpoint.mkdir()
    (source_checkpoint / "checkpoint.json").write_text("{}", encoding="utf-8")
    checkpoint = Checkpoint(source_checkpoint, (), bundle.source_row_count)
    monkeypatch.setattr(grid5000, "load_language_checkpoint", lambda *_args, **_kwargs: checkpoint)

    target = tmp_path / "copy-target"
    target.mkdir()
    grid5000._copy_checkpoint(source, target, bundle)

    assert (target / source_checkpoint.name / "checkpoint.json").read_text() == "{}"


def test_prepare_does_not_overwrite_an_existing_bundle(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    grid5000.prepare_language_bundle(run_dir, bundle_dir, model_path=model, commit="abc123")

    with pytest.raises(FileExistsError, match="already exists"):
        grid5000.prepare_language_bundle(run_dir, bundle_dir, model_path=model, commit="abc123")


def test_prepare_model_failure_does_not_advance_run_state(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)

    with pytest.raises(FileNotFoundError):
        grid5000.prepare_language_bundle(
            run_dir,
            tmp_path / "bundle",
            model_path=tmp_path / "missing-model.bin",
            commit="abc123",
        )

    assert load_run(run_dir).metadata["status"] == "enriched"


def test_sync_can_retry_a_paused_receipt_without_losing_the_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = _write_enriched_run(tmp_path, row_count=2)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123", batch_rows=1
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    clock_values = iter([0.0, 0.5, 1.1])
    grid5000.run_language_bundle(
        bundle_dir,
        time_budget_seconds=1,
        job_id="42",
        clock=lambda: next(clock_values),
    )
    grid5000.sync_language_bundle(bundle_dir, run_dir)

    result = grid5000.sync_language_bundle(bundle_dir, run_dir)

    assert result.completed is False
    assert (
        run_dir / "polygons" / ".source.parquet.language.parts" / "part-00000000.parquet"
    ).is_file()


def test_run_rejects_a_malformed_bundle_manifest(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / grid5000.BUNDLE_MANIFEST_NAME).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="run_id"):
        grid5000.run_language_bundle(bundle_dir)

    (bundle_dir / grid5000.BUNDLE_MANIFEST_NAME).write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^invalid bundle JSON:"):
        grid5000._load_bundle(bundle_dir)


def test_load_result_reports_malformed_result_json(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )
    result_path = bundle_dir / grid5000.RESULT_NAME
    result_path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match=r"^invalid result JSON:"):
        grid5000._load_result(result_path, bundle)


def test_bundle_and_result_payloads_require_the_current_schema_version(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle = grid5000.prepare_language_bundle(
        run_dir, tmp_path / "bundle", model_path=model, commit="abc123"
    )

    invalid_bundle = bundle.payload()
    invalid_bundle["schema_version"] = 99
    with pytest.raises(ValueError, match=r"^unsupported Grid'5000 bundle schema version$"):
        grid5000._bundle_from_payload(invalid_bundle)

    valid_result = grid5000.Grid5000Result(
        run_id=bundle.run_id,
        source_shard=bundle.source_shard,
        source_row_count=bundle.source_row_count,
        shard_sha256=bundle.source_shard_sha256,
        model=bundle.model,
        commit=bundle.commit,
        completed=False,
        changed=False,
        processed_rows=0,
        max_batch_rows=0,
        job_id=None,
    )
    invalid_result = valid_result.payload()
    invalid_result["schema_version"] = 99
    with pytest.raises(ValueError, match=r"^unsupported Grid'5000 bundle schema version$"):
        grid5000._result_from_payload(invalid_result)


def test_model_payload_round_trips_the_complete_identity() -> None:
    model = ModelIdentity("repo", "file", "revision", "a" * 64)

    assert grid5000._model_from_payload(grid5000._model_payload(model)) == model


def test_schema_version_rejects_true_even_though_it_compares_equal_to_one() -> None:
    with pytest.raises(ValueError, match=r"^unsupported Grid'5000 bundle schema version$"):
        grid5000._schema_version({"schema_version": True})


@pytest.mark.parametrize("value", [1, True, ""])
def test_required_string_rejects_non_string_and_empty_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^name must be a non-empty string$"):
        grid5000._required_string({"name": value}, "name")


@pytest.mark.parametrize("value", ["a" * 63, "X" * 64])
def test_sha256_value_rejects_wrong_length_and_invalid_case(value: str) -> None:
    with pytest.raises(ValueError, match=r"^digest must be a lowercase SHA-256 digest$"):
        grid5000._sha256_value({"digest": value}, "digest")


@pytest.mark.parametrize("value", [1.5, True])
def test_positive_int_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^value must be a positive integer$"):
        grid5000._positive_int({"value": value}, "value")


def test_positive_int_accepts_one() -> None:
    assert grid5000._positive_int({"value": 1}, "value") == 1


@pytest.mark.parametrize("value", [1.5, True])
def test_nonnegative_int_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^value must be a non-negative integer$"):
        grid5000._nonnegative_int({"value": value}, "value")


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("job-42", "job-42")])
def test_optional_job_id_accepts_null_and_non_empty_strings(
    value: object, expected: str | None
) -> None:
    assert grid5000._optional_job_id(value) == expected


@pytest.mark.parametrize("value", [42, ""])
def test_optional_job_id_rejects_non_string_and_empty_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^job_id must be null or a non-empty string$"):
        grid5000._optional_job_id(value)


@pytest.mark.parametrize("value", [cast(str, 42), "  "])
def test_validate_commit_rejects_non_string_and_blank_values(value: str) -> None:
    with pytest.raises(ValueError, match=r"^commit must be a non-empty string$"):
        grid5000._validate_commit(value)


@pytest.mark.parametrize("batch_rows", [1.5, True])
def test_validate_grid_options_rejects_non_integer_batch_sizes(batch_rows: object) -> None:
    with pytest.raises(ValueError, match=r"^batch_rows must be a positive integer$"):
        grid5000._validate_grid_options(1, cast(int, batch_rows))


def test_validate_positive_grid_time_accepts_one() -> None:
    grid5000._validate_positive_grid_time(1)


@pytest.mark.parametrize("value", [True, "1", float("inf"), float("nan"), 0])
def test_validate_positive_grid_time_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^time_budget_seconds must be positive$"):
        grid5000._validate_positive_grid_time(value)


def test_result_payload_validates_zero_row_and_nonzero_row_batch_bounds(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle = grid5000.prepare_language_bundle(
        run_dir, tmp_path / "bundle", model_path=model, commit="abc123"
    )
    valid_result = grid5000.Grid5000Result(
        run_id=bundle.run_id,
        source_shard=bundle.source_shard,
        source_row_count=bundle.source_row_count,
        shard_sha256=bundle.source_shard_sha256,
        model=bundle.model,
        commit=bundle.commit,
        completed=False,
        changed=False,
        processed_rows=0,
        max_batch_rows=2,
        job_id=None,
    )

    with pytest.raises(ValueError, match=r"^result batch size exceeds source row count$"):
        grid5000._result_from_payload(valid_result.payload())

    empty_result = replace(valid_result, source_row_count=0, max_batch_rows=1)
    assert grid5000._result_from_payload(empty_result.payload()).max_batch_rows == 1


def test_prepare_can_select_a_named_unfinished_shard(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")

    bundle = grid5000.prepare_language_bundle(
        run_dir,
        tmp_path / "bundle",
        model_path=model,
        commit="abc123",
        shard_name="source.parquet",
    )

    assert bundle.source_shard == "source.parquet"


def test_prepare_forwards_a_requested_shard_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    selected = run_dir / "polygons" / "source.parquet"
    observed: list[str | None] = []

    def select_source_shard(_state: RunState, *, shard_name: str | None) -> Path:
        observed.append(shard_name)
        return selected

    monkeypatch.setattr(grid5000, "_select_source_shard", select_source_shard)
    grid5000.prepare_language_bundle(
        run_dir,
        tmp_path / "bundle",
        model_path=model,
        commit="abc123",
        shard_name="source.parquet",
    )

    assert observed == ["source.parquet"]


def test_named_shard_selection_rejects_missing_complete_and_unsafe_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    state = load_run(run_dir)
    source = run_dir / "polygons" / "source.parquet"

    with pytest.raises(ValueError, match="does not exist"):
        grid5000._select_named_unfinished_shard(state, [source], "missing.parquet")
    with pytest.raises(ValueError, match="unsafe"):
        grid5000._select_named_unfinished_shard(state, [source], "../source.parquet")

    grid5000._select_named_unfinished_shard(state, [source], "source.parquet")
    with monkeypatch.context() as isolated:
        isolated.setattr(grid5000, "shard_needs_language_detection", lambda _path: False)
        with pytest.raises(ValueError, match="already complete"):
            grid5000._select_named_unfinished_shard(state, [source], "source.parquet")


@pytest.mark.parametrize(
    "status",
    [STATUS_EXTRACTED, STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE],
)
def test_prepare_run_state_enters_enriching_from_previous_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    state = RunState(tmp_path, "run", {"status": status})
    transitions: list[tuple[RunState, str]] = []
    monkeypatch.setattr(
        grid5000,
        "transition_status",
        lambda actual, new_status: transitions.append((actual, new_status)),
    )

    grid5000._prepare_run_state(state)

    assert transitions == [(state, STATUS_ENRICHING)]


@pytest.mark.parametrize("status", [STATUS_ENRICHING, STATUS_ENRICHED])
def test_prepare_run_state_keeps_resumable_states(tmp_path: Path, status: str) -> None:
    state = RunState(tmp_path, "run", {"status": status})

    grid5000._prepare_run_state(state)

    assert state.metadata["status"] == status


def test_prepare_run_state_rejects_invalid_and_frozen_states(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match=r"^Grid'5000 preparation requires an extracted/enriched run$"
    ):
        grid5000._prepare_run_state(RunState(tmp_path, "run", {"status": "initialized"}))
    with pytest.raises(ValueError, match=r"^cannot add languages to a frozen snapshot$"):
        grid5000._prepare_run_state(
            RunState(tmp_path, "run", {"status": STATUS_COMPLETE, "snapshot_status": "done"})
        )


def test_sync_state_accepts_matching_active_runs(tmp_path: Path) -> None:
    model = ModelIdentity("repo", "file", "revision", "a" * 64)
    bundle = grid5000.Grid5000Bundle(
        run_id="run",
        source_shard="source.parquet",
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=model,
        commit="abc123",
        time_budget_seconds=1,
        batch_rows=1,
    )

    for status in (STATUS_ENRICHING, STATUS_ENRICHED):
        grid5000._validate_sync_state(RunState(tmp_path, "run", {"status": status}), bundle)


@pytest.mark.parametrize(
    ("run_id", "metadata", "error"),
    [
        ("other", {"status": STATUS_ENRICHED}, "bundle run identity does not match target run"),
        (
            "run",
            {"status": STATUS_COMPLETE, "snapshot_status": "done"},
            "cannot sync languages into a frozen snapshot",
        ),
        (
            "run",
            {"status": STATUS_COMPLETE},
            "Grid'5000 synchronization requires an enriching/enriched run",
        ),
        (
            "run",
            {"status": STATUS_EXTRACTED},
            "Grid'5000 synchronization requires an enriching/enriched run",
        ),
    ],
)
def test_sync_state_rejects_mismatched_or_inactive_runs(
    tmp_path: Path,
    run_id: str,
    metadata: dict[str, str],
    error: str,
) -> None:
    bundle = grid5000.Grid5000Bundle(
        run_id="run",
        source_shard="source.parquet",
        source_row_count=1,
        source_shard_sha256="b" * 64,
        model=ModelIdentity("repo", "file", "revision", "a" * 64),
        commit="abc123",
        time_budget_seconds=1,
        batch_rows=1,
    )

    with pytest.raises(ValueError, match=rf"^{re.escape(error)}$"):
        grid5000._validate_sync_state(RunState(tmp_path, run_id, metadata), bundle)


def test_restore_directory_reinstates_the_previous_checkpoint(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "old").write_text("old", encoding="utf-8")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "saved").write_text("saved", encoding="utf-8")

    grid5000._restore_directory(target, backup)

    assert not backup.exists()
    assert (target / "saved").read_text(encoding="utf-8") == "saved"

    grid5000._restore_directory(target, None)
    assert not target.exists()


def test_remove_directory_ignores_a_missing_path(tmp_path: Path) -> None:
    grid5000._remove_directory(tmp_path / "missing")

    target = tmp_path / "target"
    target.mkdir()
    grid5000._restore_directory(target, None)
    assert not target.exists()


def test_create_bundle_directory_creates_missing_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "bundle"

    grid5000._create_bundle_directory(target)

    assert target.is_dir()


def test_backup_directory_returns_none_for_a_missing_directory(tmp_path: Path) -> None:
    assert grid5000._backup_directory(tmp_path / "missing") is None


def test_backup_directory_moves_an_existing_directory(tmp_path: Path) -> None:
    target = tmp_path / "checkpoint"
    target.mkdir()
    (target / "part").write_text("data", encoding="utf-8")

    backup = grid5000._backup_directory(target)

    assert backup is not None
    assert not target.exists()
    assert backup.name.startswith(f".{target.name}.")
    assert (backup / "part").read_text(encoding="utf-8") == "data"


def test_backup_directory_rejects_a_file_target(tmp_path: Path) -> None:
    target = tmp_path / "checkpoint"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match=r"^checkpoint target is not a directory:"):
        grid5000._backup_directory(target)


@pytest.mark.parametrize("contents", ["not json", "[]"])
def test_read_object_rejects_invalid_receipts(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "receipt.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=r"JSON must be an object|invalid"):
        grid5000._read_object(path, "receipt")

    with pytest.raises(FileNotFoundError):
        grid5000._read_object(tmp_path / "missing.json", "receipt")


def test_read_object_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(b"\xff")

    with pytest.raises(ValueError, match="invalid receipt JSON"):
        grid5000._read_object(path, "receipt")


def test_read_object_requests_explicit_utf8_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[object] = []

    def read_text(_path: Path, *, encoding: object = None) -> str:
        observed.append(encoding)
        return "{}"

    monkeypatch.setattr(grid5000.Path, "read_text", read_text)

    assert grid5000._read_object(tmp_path / "receipt.json", "receipt") == {}
    assert observed == ["utf-8"]


def test_bundle_source_validation_rejects_missing_rows_schema_and_digest(tmp_path: Path) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle = grid5000.prepare_language_bundle(
        run_dir, tmp_path / "bundle", model_path=model, commit="abc123"
    )
    source = tmp_path / "bundle" / bundle.source_shard

    with pytest.raises(FileNotFoundError) as missing:
        grid5000._validate_bundle_source(tmp_path / "missing.parquet", bundle)
    assert missing.value.args == (tmp_path / "missing.parquet",)

    wrong_rows = tmp_path / "wrong-rows.parquet"
    source_table = pq.read_table(source)
    pq.write_table(pa.concat_tables([source_table, source_table]), wrong_rows)
    with pytest.raises(ValueError, match=r"^staged source row count does not match bundle$"):
        grid5000._validate_bundle_source(wrong_rows, bundle)

    wrong_schema = tmp_path / "wrong-schema.parquet"
    pq.write_table(pa.table({"value": [1]}), wrong_schema)
    with pytest.raises(ValueError, match=r"^staged source schema is unsupported$"):
        grid5000._validate_bundle_source(wrong_schema, bundle)

    digest_mismatch = tmp_path / "digest-mismatch.parquet"
    pq.write_table(pq.read_table(source), digest_mismatch, compression="gzip")
    with pytest.raises(ValueError, match=r"^staged source hash does not match bundle$"):
        grid5000._validate_bundle_source(digest_mismatch, bundle)


def test_completed_shard_validation_rejects_row_schema_digest_and_incomplete_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _write_enriched_run(tmp_path)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle_dir = tmp_path / "bundle"
    bundle = grid5000.prepare_language_bundle(
        run_dir, bundle_dir, model_path=model, commit="abc123"
    )

    class FakeDetector:
        identity = bundle.model

        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            return [LanguagePrediction("eng_Latn", 0.9) for _text in texts]

    monkeypatch.setattr(grid5000, "load_glotlid_detector_from_path", lambda _path: FakeDetector())
    result = grid5000.run_language_bundle(bundle_dir)
    valid = bundle_dir / bundle.source_shard
    grid5000._validate_completed_shard(valid, result)

    wrong_rows = tmp_path / "completed-wrong-rows.parquet"
    table = pq.read_table(valid)
    pq.write_table(pa.concat_tables([table, table]), wrong_rows)
    with pytest.raises(
        ValueError, match=r"^completed language shard row count does not match result$"
    ):
        grid5000._validate_completed_shard(wrong_rows, result)

    wrong_schema = tmp_path / "completed-wrong-schema.parquet"
    pq.write_table(pa.table({"value": [1]}), wrong_schema)
    with pytest.raises(ValueError, match=r"^completed language shard schema mismatch$"):
        grid5000._validate_completed_shard(wrong_schema, result)

    wrong_hash = tmp_path / "completed-wrong-hash.parquet"
    pq.write_table(table, wrong_hash, compression="gzip")
    with pytest.raises(ValueError, match=r"^completed language shard hash does not match result$"):
        grid5000._validate_completed_shard(wrong_hash, result)

    incomplete = tmp_path / "completed-incomplete.parquet"
    row = table.to_pylist()[0]
    row["website_language"] = None
    row["website_language_probability"] = None
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA_V1_4), incomplete)
    incomplete_result = replace(result, shard_sha256=hash_shard(incomplete))
    with pytest.raises(ValueError, match=r"^completed language shard still needs detection$"):
        grid5000._validate_completed_shard(incomplete, incomplete_result)


def test_paused_sync_rejects_source_changes_schema_changes_and_progress_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _write_enriched_run(tmp_path, row_count=2)
    model = tmp_path / "model_v3.bin"
    model.write_bytes(b"model")
    bundle = grid5000.prepare_language_bundle(
        run_dir, tmp_path / "bundle", model_path=model, commit="abc123", batch_rows=1
    )
    local = run_dir / "polygons" / bundle.source_shard
    remote = tmp_path / "bundle" / bundle.source_shard
    result = grid5000.Grid5000Result(
        run_id=bundle.run_id,
        source_shard=bundle.source_shard,
        source_row_count=bundle.source_row_count,
        shard_sha256=bundle.source_shard_sha256,
        model=bundle.model,
        commit=bundle.commit,
        completed=False,
        changed=False,
        processed_rows=0,
        max_batch_rows=1,
        job_id=None,
    )

    local_original = local.read_bytes()
    local.write_bytes(b"changed")
    with pytest.raises(ValueError, match=r"^canonical shard changed since bundle preparation$"):
        grid5000._sync_paused_checkpoint(local, remote, bundle, result)
    local.write_bytes(local_original)

    remote_original = remote.read_bytes()
    remote.write_bytes(b"changed")
    with pytest.raises(ValueError, match=r"^paused bundle source changed unexpectedly$"):
        grid5000._sync_paused_checkpoint(local, remote, bundle, result)
    remote.write_bytes(remote_original)

    with monkeypatch.context() as isolated:
        isolated.setattr(grid5000, "is_current_public_polygon_schema", lambda _schema: False)
        with pytest.raises(ValueError, match=r"^paused bundle source schema is unsupported$"):
            grid5000._sync_paused_checkpoint(local, remote, bundle, result)

    with pytest.raises(ValueError, match=r"^paused result does not match checkpoint progress$"):
        grid5000._sync_paused_checkpoint(local, remote, bundle, replace(result, processed_rows=1))
