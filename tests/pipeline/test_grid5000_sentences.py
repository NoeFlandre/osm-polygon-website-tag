"""Tests for the offline Grid'5000 sentence-segmentation bundle boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
    schema_matches,
)
from osm_polygon_website_tag.pipeline import grid5000_sentences
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.pipeline.sentence_checkpoint import sentence_checkpoint_store
from osm_polygon_website_tag.pipeline.split_sentences import SentenceSegmentationResult
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_COMPLETE,
    atomic_write_json,
    hash_shard,
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
    transition_status,
)

MODEL_REVISION = "137da05"


class _FakeSplitter:
    """Deterministic splitter standing in for the pinned SaT model."""

    def __init__(self, identity: ModelIdentity) -> None:
        self._identity = identity
        self.calls = 0

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    def split(self, texts: Sequence[str]) -> list[list[str]]:
        self.calls += 1
        return [text.split("|") for text in texts]


def _row(index: int, *, language: str = "eng_Latn") -> dict[str, object]:
    values: dict[str, object] = {}
    for field in POLYGON_PUBLIC_SCHEMA_V1_4:
        if field.name == "polygon_id":
            values[field.name] = f"source:way/{index}"
        elif pa.types.is_boolean(field.type):
            values[field.name] = False
        elif pa.types.is_integer(field.type):
            values[field.name] = 0
        elif pa.types.is_floating(field.type):
            values[field.name] = 0.0
        elif pa.types.is_timestamp(field.type):
            values[field.name] = pa.scalar(0, type=field.type).as_py()
        else:
            values[field.name] = ""
    values.update(
        has_any_website=True,
        has_website=True,
        website="https://example.org",
        website_text="One|Two",
        website_text_status="success",
        website_language=language,
        website_language_probability=0.99,
        contact_website_text=None,
        contact_website_text_status="absent",
        contact_website_language=None,
        contact_website_language_probability=None,
        schema_version="v1.4",
    )
    return values


def _write_shard(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA_V1_4), path)
    return path


def _write_language_run(tmp_path: Path, *, shards: dict[str, int], run_id: str = "run") -> Path:
    """Create an enriched run whose shards all carry complete v1.4 languages."""
    run_dir, state = initialise_run(tmp_path / "runs", run_id=run_id)
    transition_status(state, "extracting")
    transition_status(state, "extracted")
    transition_status(state, "enriching")
    for stem, row_count in shards.items():
        source = tmp_path / f"{stem}.osm.pbf"
        source.write_bytes(stem.encode())
        shard = _write_shard(
            run_dir / "polygons" / f"{stem}.parquet", [_row(index) for index in range(row_count)]
        )
        record_processed_source(
            state,
            snapshot_source_fingerprint(source),
            public_row_count=row_count,
            public_shard_sha256=hash_shard(shard),
        )
    transition_status(state, "enriched")
    return run_dir


def _write_model(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text('{"model": "sat"}', encoding="utf-8")
    (directory / "model.safetensors").write_bytes(b"weights")
    return directory


def _prepare(
    tmp_path: Path,
    *,
    shards: dict[str, int],
    max_rows: int = 1_000,
    bundle_name: str = "bundle",
) -> tuple[Path, Path, grid5000_sentences.SentenceBundle]:
    run_dir = _write_language_run(tmp_path, shards=shards)
    model_dir = _write_model(tmp_path / "sat-3l-sm")
    bundle_dir = tmp_path / bundle_name
    bundle = grid5000_sentences.prepare_sentence_bundle(
        run_dir,
        bundle_dir,
        model_dir=model_dir,
        model_revision=MODEL_REVISION,
        commit="abc123",
        max_rows=max_rows,
    )
    return run_dir, bundle_dir, bundle


def _run_with(
    bundle_dir: Path,
    *,
    time_budget_seconds: float | None = None,
    batch_rows: int | None = None,
    job_id: str | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[grid5000_sentences.SentenceBundleResult, _FakeSplitter]:
    bundle = grid5000_sentences.load_sentence_bundle(bundle_dir)
    splitter = _FakeSplitter(bundle.model)
    result = grid5000_sentences.run_sentence_bundle(
        bundle_dir,
        splitter=splitter,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
        job_id=job_id,
        clock=clock,
    )
    return result, splitter


def _run(
    bundle_dir: Path,
    *,
    time_budget_seconds: float | None = None,
    batch_rows: int | None = None,
    job_id: str | None = None,
    clock: Callable[[], float] | None = None,
) -> grid5000_sentences.SentenceBundleResult:
    return _run_with(
        bundle_dir,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
        job_id=job_id,
        clock=clock,
    )[0]


def test_prepare_packs_unfinished_shards_and_stages_the_model(tmp_path: Path) -> None:
    run_dir, bundle_dir, bundle = _prepare(tmp_path, shards={"alpha": 2, "beta": 3})

    assert [entry.name for entry in bundle.shards] == ["alpha.parquet", "beta.parquet"]
    assert [entry.row_count for entry in bundle.shards] == [2, 3]
    assert bundle.shards[0].sha256 == hash_shard(run_dir / "polygons" / "alpha.parquet")
    assert bundle.model.repository == "segment-any-text/sat-3l-sm"
    assert bundle.model.revision == MODEL_REVISION
    assert bundle.commit == "abc123"
    assert (bundle_dir / "sat-3l-sm" / "model.safetensors").read_bytes() == b"weights"
    assert (bundle_dir / "alpha.parquet").is_file()
    assert (bundle_dir / "beta.parquet").is_file()


def test_prepare_stops_packing_at_the_row_budget(tmp_path: Path) -> None:
    _, _, bundle = _prepare(tmp_path, shards={"alpha": 2, "beta": 3, "gamma": 4}, max_rows=5)

    assert [entry.name for entry in bundle.shards] == ["alpha.parquet", "beta.parquet"]


def test_prepare_always_packs_at_least_one_shard(tmp_path: Path) -> None:
    _, _, bundle = _prepare(tmp_path, shards={"alpha": 9}, max_rows=1)

    assert [entry.name for entry in bundle.shards] == ["alpha.parquet"]


def test_prepare_refuses_a_run_without_unfinished_shards(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)

    assert result.completed
    with pytest.raises(ValueError, match="already carry sentences"):
        grid5000_sentences.prepare_sentence_bundle(
            run_dir,
            tmp_path / "second",
            model_dir=tmp_path / "sat-3l-sm",
            model_revision=MODEL_REVISION,
            commit="abc123",
        )


def test_prepare_refuses_an_existing_bundle_directory(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})

    with pytest.raises(FileExistsError):
        grid5000_sentences.prepare_sentence_bundle(
            run_dir,
            bundle_dir,
            model_dir=tmp_path / "sat-3l-sm",
            model_revision=MODEL_REVISION,
            commit="abc123",
        )


def test_run_segments_every_staged_shard_and_writes_one_receipt(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 2, "beta": 1})

    result = _run(bundle_dir, job_id="4242")

    assert result.completed
    assert result.job_id == "4242"
    assert [outcome.name for outcome in result.shards] == ["alpha.parquet", "beta.parquet"]
    assert all(outcome.completed and outcome.changed for outcome in result.shards)
    assert result.shards[0].processed_rows == 2
    staged = pq.ParquetFile(bundle_dir / "alpha.parquet")
    assert schema_matches(staged.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_5)
    receipt = json.loads((bundle_dir / "result.json").read_text(encoding="utf-8"))
    assert receipt["completed"] is True
    assert receipt["shards"][0]["shard_sha256"] == hash_shard(bundle_dir / "alpha.parquet")


def test_run_reuses_an_existing_receipt_without_resegmenting(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    first = _run(bundle_dir)

    bundle = grid5000_sentences.load_sentence_bundle(bundle_dir)
    splitter = _FakeSplitter(bundle.model)
    second = grid5000_sentences.run_sentence_bundle(bundle_dir, splitter=splitter)

    assert splitter.calls == 0
    assert second == first


def test_run_rejects_a_splitter_that_is_not_the_staged_model(tmp_path: Path) -> None:
    _, bundle_dir, bundle = _prepare(tmp_path, shards={"alpha": 1})
    other = ModelIdentity(bundle.model.repository, bundle.model.filename, "other", "b" * 64)

    with pytest.raises(ValueError, match="model identity"):
        grid5000_sentences.run_sentence_bundle(bundle_dir, splitter=_FakeSplitter(other))


def test_run_rejects_a_staged_shard_that_changed(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _write_shard(bundle_dir / "alpha.parquet", [_row(0), _row(1)])

    with pytest.raises(ValueError, match="staged shard"):
        _run(bundle_dir)


def test_run_pauses_on_an_exhausted_budget_and_reports_partial_progress(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 2, "beta": 2})
    ticks = iter([0.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])

    result = _run(bundle_dir, time_budget_seconds=5.0, batch_rows=1, clock=lambda: next(ticks))

    assert result.completed is False
    assert result.shards[0].completed is False
    assert result.shards[0].processed_rows < 2


def test_sync_installs_completed_shards_and_updates_the_manifest(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 2, "beta": 1})
    result = _run(bundle_dir)

    synced = grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)

    assert synced == result
    local = run_dir / "polygons" / "alpha.parquet"
    assert schema_matches(pq.ParquetFile(local).schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_5)
    state = load_run(run_dir)
    assert state.sources["alpha.osm.pbf"]["public_shard_sha256"] == hash_shard(local)
    history = sorted((run_dir / "manifests" / "grid5000-sentences").glob("*.json"))
    assert len(history) == 1
    assert json.loads(history[0].read_text(encoding="utf-8"))["action"] == "completed"


def test_sync_installs_only_a_checkpoint_for_a_paused_job(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 4})
    local = run_dir / "polygons" / "alpha.parquet"
    before = hash_shard(local)
    ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0])

    result = _run(bundle_dir, time_budget_seconds=5.0, batch_rows=1, clock=lambda: next(ticks))
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)

    assert result.completed is False
    assert hash_shard(local) == before
    assert sentence_checkpoint_store().directory_for(local).is_dir()
    history = sorted((run_dir / "manifests" / "grid5000-sentences").glob("*.json"))
    assert json.loads(history[0].read_text(encoding="utf-8"))["action"] == "paused"


def test_sync_resumes_a_paused_shard_in_the_next_bundle(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 4})
    ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0])
    _run(bundle_dir, time_budget_seconds=5.0, batch_rows=1, clock=lambda: next(ticks))
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)

    second_dir = tmp_path / "bundle-2"
    grid5000_sentences.prepare_sentence_bundle(
        run_dir,
        second_dir,
        model_dir=tmp_path / "sat-3l-sm",
        model_revision=MODEL_REVISION,
        commit="abc123",
    )
    result, splitter = _run_with(second_dir, batch_rows=1)
    grid5000_sentences.sync_sentence_bundle(second_dir, run_dir)

    assert result.completed
    assert result.shards[0].processed_rows == 4
    assert splitter.calls == 3
    local = run_dir / "polygons" / "alpha.parquet"
    assert schema_matches(pq.ParquetFile(local).schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_5)
    assert not sentence_checkpoint_store().directory_for(local).exists()


def test_sync_refuses_a_receipt_from_another_run(tmp_path: Path) -> None:
    _run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    other = _write_language_run(tmp_path / "other", shards={"alpha": 1}, run_id="other")

    with pytest.raises(ValueError, match="run identity"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, other)


def test_sync_refuses_a_frozen_snapshot(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    state = load_run(run_dir)
    state.metadata["status"] = STATUS_COMPLETE
    state.metadata["snapshot_status"] = "done"
    atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)

    with pytest.raises(ValueError, match="frozen snapshot"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


def test_sync_refuses_a_canonical_shard_that_changed_since_preparation(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    _write_shard(run_dir / "polygons" / "alpha.parquet", [_row(0), _row(1)])

    with pytest.raises(ValueError, match="canonical shard changed"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


def _paused_bundle(tmp_path: Path) -> tuple[Path, Path]:
    """Prepare, pause, and return one bundle with a durable partial prefix."""
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 4})
    ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0])
    _run(bundle_dir, time_budget_seconds=5.0, batch_rows=1, clock=lambda: next(ticks))
    return run_dir, bundle_dir


def test_sync_refuses_a_paused_bundle_whose_staged_shard_changed(tmp_path: Path) -> None:
    run_dir, bundle_dir = _paused_bundle(tmp_path)
    _write_shard(bundle_dir / "alpha.parquet", [_row(0)])

    with pytest.raises(ValueError, match="paused bundle source changed"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


def test_sync_refuses_a_paused_bundle_without_a_checkpoint(tmp_path: Path) -> None:
    run_dir, bundle_dir = _paused_bundle(tmp_path)
    shutil.rmtree(sentence_checkpoint_store().directory_for(bundle_dir / "alpha.parquet"))

    with pytest.raises(ValueError, match="no checkpoint"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


def test_sync_refuses_a_paused_checkpoint_that_disagrees_with_the_receipt(
    tmp_path: Path,
) -> None:
    run_dir, bundle_dir = _paused_bundle(tmp_path)
    receipt = json.loads((bundle_dir / "result.json").read_text(encoding="utf-8"))
    receipt["shards"][0]["processed_rows"] += 1
    (bundle_dir / "result.json").write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError, match="checkpoint progress"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


def test_sync_refuses_a_paused_bundle_whose_canonical_shard_changed(tmp_path: Path) -> None:
    run_dir, bundle_dir = _paused_bundle(tmp_path)
    _write_shard(run_dir / "polygons" / "alpha.parquet", [_row(0)])

    with pytest.raises(ValueError, match="canonical shard changed"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("commit", "other", "result commit"),
        ("model", "other", "result model identity"),
    ],
)
def test_sync_refuses_a_receipt_bound_to_other_inputs(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    run_dir, bundle_dir, _bundle = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    receipt = json.loads((bundle_dir / "result.json").read_text(encoding="utf-8"))
    if field == "model":
        receipt["model"] = {**receipt["model"], "revision": value}
    else:
        receipt[field] = value
    (bundle_dir / "result.json").write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


def _entry_payload() -> dict[str, object]:
    return {"name": "alpha.parquet", "row_count": 2, "sha256": "a" * 64}


def _outcome_payload() -> dict[str, object]:
    return {
        "changed": True,
        "completed": True,
        "max_batch_rows": 2,
        "name": "alpha.parquet",
        "processed_rows": 2,
        "row_count": 2,
        "shard_sha256": "b" * 64,
    }


def test_shard_entry_payload_round_trips() -> None:
    entry = grid5000_sentences._shard_entry_from_payload(_entry_payload())

    assert entry == grid5000_sentences.SentenceShardEntry("alpha.parquet", 2, "a" * 64)
    assert entry.payload() == _entry_payload()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../escape.parquet"),
        ("name", "alpha.txt"),
        ("name", ""),
        ("row_count", -1),
        ("row_count", True),
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
    ],
)
def test_shard_entry_payload_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        grid5000_sentences._shard_entry_from_payload({**_entry_payload(), field: value})


def test_shard_entry_payload_rejects_a_non_object() -> None:
    with pytest.raises(ValueError, match=r"^shard entry must be an object$"):
        grid5000_sentences._shard_entry_from_payload(["alpha.parquet"])


def test_outcome_payload_round_trips() -> None:
    outcome = grid5000_sentences._outcome_from_payload(_outcome_payload())

    assert outcome.payload() == _outcome_payload()
    assert outcome.name == "alpha.parquet"
    assert outcome.row_count == 2
    assert outcome.shard_sha256 == "b" * 64
    assert outcome.completed is True
    assert outcome.changed is True
    assert outcome.processed_rows == 2
    assert outcome.max_batch_rows == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../escape.parquet"),
        ("name", ""),
        ("row_count", -1),
        ("shard_sha256", "z" * 64),
        ("completed", "true"),
        ("changed", None),
        ("processed_rows", -1),
        ("max_batch_rows", -1),
    ],
)
def test_outcome_payload_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        grid5000_sentences._outcome_from_payload({**_outcome_payload(), field: value})


def test_outcome_payload_rejects_a_non_object() -> None:
    with pytest.raises(ValueError, match=r"^shard outcome must be an object$"):
        grid5000_sentences._outcome_from_payload("alpha.parquet")


def test_outcome_list_parsers_require_the_documented_shapes() -> None:
    assert grid5000_sentences._outcomes_from_payload([]) == ()
    assert grid5000_sentences._outcomes_from_payload([_outcome_payload()])[0].name == (
        "alpha.parquet"
    )
    with pytest.raises(ValueError, match=r"^shards must be a list$"):
        grid5000_sentences._outcomes_from_payload({})
    with pytest.raises(ValueError, match=r"^shards must be a non-empty list$"):
        grid5000_sentences._shards_from_payload([])
    with pytest.raises(ValueError, match=r"^shards must be a non-empty list$"):
        grid5000_sentences._shards_from_payload({})
    assert grid5000_sentences._shards_from_payload([_entry_payload()])[0].row_count == 2


def test_outcome_projects_a_segmentation_result(tmp_path: Path) -> None:
    result = SentenceSegmentationResult(
        shard_path=tmp_path / "alpha.parquet",
        row_count=7,
        changed=True,
        shard_sha256="c" * 64,
        max_batch_rows=3,
        processed_rows=5,
        completed=False,
    )

    outcome = grid5000_sentences._outcome(tmp_path / "alpha.parquet", result)

    assert outcome == grid5000_sentences.SentenceShardOutcome(
        name="alpha.parquet",
        row_count=7,
        shard_sha256="c" * 64,
        completed=False,
        changed=True,
        processed_rows=5,
        max_batch_rows=3,
    )


def test_checkpoint_rows_counts_every_durable_part(tmp_path: Path) -> None:
    directory = tmp_path / "parts"
    directory.mkdir()
    _write_shard(directory / "0000.parquet", [_row(0), _row(1)])
    _write_shard(directory / "0001.parquet", [_row(2)])

    assert grid5000_sentences._checkpoint_rows(directory) == 3
    assert grid5000_sentences._checkpoint_rows(tmp_path / "empty") == 0


def test_validate_completed_shard_checks_rows_schema_and_digest(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)
    shard = bundle_dir / "alpha.parquet"
    outcome = result.shards[0]

    grid5000_sentences._validate_completed_shard(shard, outcome)

    with pytest.raises(ValueError, match="row count does not match"):
        grid5000_sentences._validate_completed_shard(
            shard, replace(outcome, row_count=outcome.row_count + 1)
        )
    with pytest.raises(ValueError, match="hash does not match"):
        grid5000_sentences._validate_completed_shard(shard, replace(outcome, shard_sha256="d" * 64))
    unsegmented = _write_shard(tmp_path / "v14.parquet", [_row(0)])
    with pytest.raises(ValueError, match="schema mismatch"):
        grid5000_sentences._validate_completed_shard(
            unsegmented,
            replace(outcome, row_count=1, shard_sha256=hash_shard(unsegmented)),
        )


def test_install_completed_shard_promotes_and_clears_the_checkpoint(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)
    local = run_dir / "polygons" / "alpha.parquet"
    checkpoint = sentence_checkpoint_store().directory_for(local)
    checkpoint.mkdir(parents=True, exist_ok=True)

    grid5000_sentences._install_completed_shard(
        local, bundle_dir / "alpha.parquet", result.shards[0]
    )

    assert hash_shard(local) == result.shards[0].shard_sha256
    assert not checkpoint.exists()
    assert not local.with_name(f".{local.name}.grid5000-syncing").exists()


def test_install_completed_shard_leaves_the_canonical_shard_on_a_bad_receipt(
    tmp_path: Path,
) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)
    local = run_dir / "polygons" / "alpha.parquet"
    before = hash_shard(local)

    with pytest.raises(ValueError):
        grid5000_sentences._install_completed_shard(
            local,
            bundle_dir / "alpha.parquet",
            replace(result.shards[0], shard_sha256="e" * 64),
        )

    assert hash_shard(local) == before
    assert not local.with_name(f".{local.name}.grid5000-syncing").exists()


def test_write_sync_history_records_the_action_bundle_and_result(tmp_path: Path) -> None:
    run_dir, bundle_dir, bundle = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)

    grid5000_sentences._write_sync_history(run_dir, bundle, result)

    history = sorted((run_dir / "manifests" / "grid5000-sentences").glob("*.json"))
    assert len(history) == 1
    assert history[0].name.startswith("alpha-")
    assert len(history[0].stem.split("-")[-1]) == 16
    payload = json.loads(history[0].read_text(encoding="utf-8"))
    assert payload["action"] == "completed"
    assert payload["bundle"] == bundle.payload()
    assert payload["result"] == result.payload()

    paused = replace(result, completed=False)
    grid5000_sentences._write_sync_history(run_dir, bundle, paused)
    actions = {
        json.loads(path.read_text(encoding="utf-8"))["action"]
        for path in (run_dir / "manifests" / "grid5000-sentences").glob("*.json")
    }
    assert actions == {"completed", "paused"}


def test_finish_sync_state_transitions_only_when_every_shard_is_segmented(
    tmp_path: Path,
) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1, "beta": 1})
    state = load_run(run_dir)
    state.metadata["status"] = "enriching"

    grid5000_sentences._finish_sync_state(state)
    assert state.metadata["status"] == "enriching"

    _run(bundle_dir)
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)
    finished = load_run(run_dir)

    assert finished.metadata["status"] == "enriched"


def test_finish_sync_state_leaves_a_non_enriching_run_untouched(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)
    state = load_run(run_dir)
    state.metadata["status"] = "analyzed"

    grid5000_sentences._finish_sync_state(state)

    assert state.metadata["status"] == "analyzed"


def test_validate_result_identity_rejects_every_mismatch(tmp_path: Path) -> None:
    _, bundle_dir, bundle = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)

    grid5000_sentences._validate_result_identity(result, bundle)

    with pytest.raises(ValueError, match=r"^result run identity does not match bundle$"):
        grid5000_sentences._validate_result_identity(replace(result, run_id="other"), bundle)
    with pytest.raises(ValueError, match=r"^result model identity does not match bundle$"):
        grid5000_sentences._validate_result_identity(
            replace(result, model=replace(bundle.model, revision="other")), bundle
        )
    with pytest.raises(ValueError, match=r"^result commit does not match bundle$"):
        grid5000_sentences._validate_result_identity(replace(result, commit="other"), bundle)


def test_validate_outcome_binding_requires_a_staged_shard(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    outcome = _run(bundle_dir).shards[0]

    grid5000_sentences._validate_outcome_binding(outcome, {"alpha.parquet": outcome.row_count})

    with pytest.raises(ValueError, match="not in the bundle"):
        grid5000_sentences._validate_outcome_binding(outcome, {})
    with pytest.raises(ValueError, match="row count does not match bundle"):
        grid5000_sentences._validate_outcome_binding(
            outcome, {"alpha.parquet": outcome.row_count + 1}
        )


def test_prepare_refuses_a_frozen_snapshot(tmp_path: Path) -> None:
    run_dir = _write_language_run(tmp_path, shards={"alpha": 1})
    state = load_run(run_dir)
    state.metadata["status"] = STATUS_COMPLETE
    state.metadata["snapshot_status"] = "done"
    atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)

    with pytest.raises(ValueError, match=r"^cannot add sentences to a frozen snapshot$"):
        grid5000_sentences.prepare_sentence_bundle(
            run_dir,
            tmp_path / "bundle",
            model_dir=_write_model(tmp_path / "sat-3l-sm"),
            model_revision=MODEL_REVISION,
            commit="abc123",
        )


def test_prepare_accepts_a_complete_run_that_was_never_frozen(tmp_path: Path) -> None:
    run_dir = _write_language_run(tmp_path, shards={"alpha": 1})
    state = load_run(run_dir)
    state.metadata["status"] = STATUS_COMPLETE
    atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)

    bundle = grid5000_sentences.prepare_sentence_bundle(
        run_dir,
        tmp_path / "bundle",
        model_dir=_write_model(tmp_path / "sat-3l-sm"),
        model_revision=MODEL_REVISION,
        commit="abc123",
    )

    assert bundle.shards[0].name == "alpha.parquet"
    assert load_run(run_dir).metadata["status"] == "enriching"


def test_sync_reports_the_exact_frozen_and_identity_messages(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    other = _write_language_run(tmp_path / "other", shards={"alpha": 1}, run_id="other")

    with pytest.raises(ValueError, match=r"^bundle run identity does not match target run$"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, other)

    state = load_run(run_dir)
    state.metadata["status"] = STATUS_COMPLETE
    state.metadata["snapshot_status"] = "done"
    atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)

    with pytest.raises(ValueError, match=r"^cannot sync sentences into a frozen snapshot$"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)


def test_run_reports_the_exact_model_mismatch_message(tmp_path: Path) -> None:
    _, bundle_dir, bundle = _prepare(tmp_path, shards={"alpha": 1})
    other = ModelIdentity(bundle.model.repository, bundle.model.filename, "other", "b" * 64)

    with pytest.raises(ValueError, match=r"^staged model identity does not match bundle$"):
        grid5000_sentences.run_sentence_bundle(bundle_dir, splitter=_FakeSplitter(other))


def test_prepare_reports_the_exact_finished_run_message(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)

    with pytest.raises(ValueError, match=r"^all public shards already carry sentences$"):
        grid5000_sentences.prepare_sentence_bundle(
            run_dir,
            tmp_path / "second",
            model_dir=tmp_path / "sat-3l-sm",
            model_revision=MODEL_REVISION,
            commit="abc123",
        )


def test_malformed_manifests_name_the_document_they_came_from(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    result_path = bundle_dir / "result.json"
    result_path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid result JSON"):
        grid5000_sentences.sync_sentence_bundle(bundle_dir, tmp_path / "runs" / "run")

    (bundle_dir / "bundle.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid bundle JSON"):
        grid5000_sentences.load_sentence_bundle(bundle_dir)


def test_receipt_round_trip_preserves_the_job_identifier(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir, job_id="4242")

    reloaded = grid5000_sentences.sync_sentence_bundle(bundle_dir, tmp_path / "runs" / "run")

    assert reloaded.job_id == "4242"


def test_receipts_reject_an_empty_job_identifier(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})

    with pytest.raises(ValueError, match="job_id must be null or a non-empty string"):
        _run(bundle_dir, job_id="")


def test_receipt_payload_rejects_an_empty_job_identifier() -> None:
    payload = {
        "commit": "abc123",
        "completed": True,
        "job_id": "",
        "model": {
            "filename": "sat-3l-sm",
            "repository": "segment-any-text/sat-3l-sm",
            "revision": MODEL_REVISION,
            "sha256": "a" * 64,
        },
        "run_id": "run",
        "schema_version": 1,
        "shards": [],
    }

    with pytest.raises(ValueError, match="job_id must be null or a non-empty string"):
        grid5000_sentences._result_from_payload(payload)

    assert grid5000_sentences._result_from_payload({**payload, "job_id": "7"}).job_id == "7"
    assert grid5000_sentences._result_from_payload({**payload, "job_id": None}).job_id is None


def test_packed_shards_accumulates_every_selected_shard(tmp_path: Path) -> None:
    _, _, bundle = _prepare(tmp_path, shards={"alpha": 2, "beta": 2, "gamma": 2}, max_rows=5)

    assert [entry.name for entry in bundle.shards] == ["alpha.parquet", "beta.parquet"]


def test_stage_sources_requires_one_entry_per_selected_shard(tmp_path: Path) -> None:
    run_dir, _bundle_dir, bundle = _prepare(tmp_path, shards={"alpha": 1})
    extra = run_dir / "polygons" / "alpha.parquet"
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ValueError, match="zip"):
        grid5000_sentences._stage_sources([extra, extra], bundle, staging)


def test_prepare_reuses_model_storage_on_the_same_filesystem(tmp_path: Path) -> None:
    _, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})

    staged = bundle_dir / "sat-3l-sm" / "model.safetensors"
    assert staged.samefile(tmp_path / "sat-3l-sm" / "model.safetensors")


def test_sync_records_the_row_count_from_the_receipt(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 3})
    result = _run(bundle_dir)

    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)

    source = load_run(run_dir).sources["alpha.osm.pbf"]
    assert source["public_row_count"] == result.shards[0].row_count == 3


def test_install_completed_shard_tolerates_a_missing_checkpoint(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)
    local = run_dir / "polygons" / "alpha.parquet"
    assert not sentence_checkpoint_store().directory_for(local).exists()

    grid5000_sentences._install_completed_shard(
        local, bundle_dir / "alpha.parquet", result.shards[0]
    )

    assert hash_shard(local) == result.shards[0].shard_sha256


def test_finish_sync_state_ignores_a_run_without_shards(tmp_path: Path) -> None:
    run_dir = _write_language_run(tmp_path, shards={"alpha": 1})
    (run_dir / "polygons" / "alpha.parquet").unlink()
    state = load_run(run_dir)
    state.metadata["status"] = "enriching"

    grid5000_sentences._finish_sync_state(state)

    assert state.metadata["status"] == "enriching"


def test_write_sync_history_names_files_by_shard_and_receipt_digest(tmp_path: Path) -> None:
    _, bundle_dir, bundle = _prepare(tmp_path, shards={"alpha": 1})
    result = _run(bundle_dir)
    fresh = tmp_path / "fresh-run"

    grid5000_sentences._write_sync_history(fresh, bundle, result)

    digest = hashlib.sha256(
        json.dumps(result.payload(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    assert (fresh / "manifests" / "grid5000-sentences" / f"alpha-{digest}.json").is_file()


def test_receipt_digest_is_short_and_key_order_independent() -> None:
    digest = grid5000_sentences._receipt_digest({"b": 1, "a": 2})

    assert len(digest) == 16
    assert digest == grid5000_sentences._receipt_digest({"a": 2, "b": 1})
    assert digest != grid5000_sentences._receipt_digest({"a": 2, "b": 3})
    assert digest == hashlib.sha256(b'{"a":2,"b":1}').hexdigest()[:16]


def test_finish_sync_state_completes_a_fully_segmented_run(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1})
    _run(bundle_dir)
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)
    state = load_run(run_dir)
    state.metadata["status"] = "enriching"
    atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)

    grid5000_sentences._finish_sync_state(state)

    assert state.metadata["status"] == "enriched"


def test_finish_sync_state_waits_for_every_shard(tmp_path: Path) -> None:
    run_dir, bundle_dir, _ = _prepare(tmp_path, shards={"alpha": 1, "beta": 1}, max_rows=1)
    _run(bundle_dir)
    grid5000_sentences.sync_sentence_bundle(bundle_dir, run_dir)
    state = load_run(run_dir)
    state.metadata["status"] = "enriching"

    grid5000_sentences._finish_sync_state(state)

    assert state.metadata["status"] == "enriching"
