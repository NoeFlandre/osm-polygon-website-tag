"""Bounded, resumable sentence segmentation for one public polygon shard."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.split_sentences as split_sentences
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
    schema_matches,
)
from osm_polygon_website_tag.contracts.sentence_schema import (
    SENTENCE_ABSENT,
    SENTENCE_SUCCESS,
    SENTENCE_UNSUPPORTED_LANGUAGE,
)
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.pipeline.split_sentences import (
    SentenceSegmentationResult,
    segment_sentence_shard,
    shard_needs_sentence_segmentation,
)
from osm_polygon_website_tag.runtime.run_state import hash_shard


class _FakeSplitter:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity("segment-any-text/sat-3l-sm", "sat-3l-sm", "abc1234", "a" * 64)

    def split(self, texts: Sequence[str]) -> list[list[str]]:
        self.calls += 1
        return [text.split("|") for text in texts]


def _row(index: int, *, language: str | None = "eng_Latn", text: str = "One|Two") -> dict:
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
        website_text=text,
        website_text_status="success",
        website_language=language,
        website_language_probability=0.99,
        contact_website_text_status="absent",
        contact_website_text=None,
        contact_website_language=None,
        contact_website_language_probability=None,
        schema_version="v1.4",
    )
    return values


def _write(shard: Path, rows: list[dict], schema: pa.Schema = POLYGON_PUBLIC_SCHEMA_V1_4) -> Path:
    shard.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), shard)
    return shard


def test_v1_4_shard_is_promoted_to_v1_5_with_sentences(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0), _row(1)])

    result = segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=2)

    assert result == SentenceSegmentationResult(
        shard_path=shard,
        row_count=2,
        changed=True,
        shard_sha256=hash_shard(shard),
        max_batch_rows=2,
        processed_rows=2,
        completed=True,
    )
    table = pq.read_table(shard)
    assert schema_matches(pq.read_schema(shard), POLYGON_PUBLIC_SCHEMA_V1_5)
    rows = table.to_pylist()
    assert rows[0]["website_sentences"] == ["One", "Two"]
    assert rows[0]["website_sentence_count"] == 2
    assert rows[0]["website_sentence_status"] == SENTENCE_SUCCESS
    assert rows[0]["contact_website_sentence_status"] == SENTENCE_ABSENT
    assert rows[0]["schema_version"] == "v1.5"


def test_a_language_the_model_does_not_cover_is_recorded(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0, language="zza_Latn")])

    segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=1)

    row = pq.read_table(shard).to_pylist()[0]
    assert row["website_sentences"] is None
    assert row["website_sentence_status"] == SENTENCE_UNSUPPORTED_LANGUAGE


def test_a_completed_v1_5_shard_is_left_untouched(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0)])
    segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=1)
    before = shard.read_bytes()
    splitter = _FakeSplitter()

    result = segment_sentence_shard(shard, splitter=splitter, batch_rows=1)

    assert result == SentenceSegmentationResult(
        shard_path=shard,
        row_count=1,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=0,
        processed_rows=1,
        completed=True,
    )
    assert splitter.calls == 0
    assert shard.read_bytes() == before


def test_shard_needs_segmentation_reflects_the_schema(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0)])
    assert shard_needs_sentence_segmentation(shard) is True

    segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=1)
    assert shard_needs_sentence_segmentation(shard) is False


def test_a_shard_without_language_columns_is_rejected(tmp_path: Path) -> None:
    row = {name: value for name, value in _row(0).items() if "language" not in name}
    row["schema_version"] = "v1.3"
    shard = _write(tmp_path / "region.parquet", [row], schema=POLYGON_PUBLIC_SCHEMA)

    with pytest.raises(
        ValueError,
        match=rf"^{re.escape('unsupported polygon schema for segmentation: region.parquet')}$",
    ):
        segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=1)


def test_a_deadline_pauses_without_touching_the_source_shard(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0), _row(1)])
    before = shard.read_bytes()

    # The budget is relative to the start, so the clock must actually advance.
    ticks = iter([0.0, 99.0])
    result = segment_sentence_shard(
        shard,
        splitter=_FakeSplitter(),
        batch_rows=1,
        time_budget_seconds=1.0,
        clock=lambda: next(ticks, 99.0),
    )

    assert result == SentenceSegmentationResult(
        shard_path=shard,
        row_count=2,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=0,
        processed_rows=0,
        completed=False,
    )
    assert shard.read_bytes() == before


def test_a_paused_run_resumes_from_its_durable_prefix(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0), _row(1)])
    # Deadline, then one batch inside the budget, then the budget is spent.
    clock = iter([0.0, 0.0, 99.0])

    paused = segment_sentence_shard(
        shard,
        splitter=_FakeSplitter(),
        batch_rows=1,
        time_budget_seconds=10.0,
        clock=lambda: next(clock, 99.0),
    )
    assert paused == SentenceSegmentationResult(
        shard_path=shard,
        row_count=2,
        changed=False,
        shard_sha256=hash_shard(shard),
        max_batch_rows=1,
        processed_rows=1,
        completed=False,
    )

    resumed = segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=1)

    assert resumed.completed is True
    rows = pq.read_table(shard).to_pylist()
    assert [row["website_sentence_count"] for row in rows] == [2, 2]


def test_batch_rows_must_be_positive(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0)])

    with pytest.raises(ValueError, match=rf"^{re.escape('batch_rows must be positive')}$"):
        segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=0)


def test_a_non_positive_time_budget_is_rejected(tmp_path: Path) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0)])

    for budget in (0.0, -1.0):
        with pytest.raises(
            ValueError, match=rf"^{re.escape('time_budget_seconds must be positive')}$"
        ):
            segment_sentence_shard(
                shard, splitter=_FakeSplitter(), batch_rows=1, time_budget_seconds=budget
            )


def test_skip_checkpointed_rows_covers_whole_partial_and_empty_prefixes() -> None:
    originals: list[dict] = [{"id": 1}, {"id": 2}]

    assert split_sentences._skip_checkpointed_rows(originals, 3) == ([], 1)
    # Exactly the batch width must consume the whole batch, not fall through.
    assert split_sentences._skip_checkpointed_rows(originals, 2) == ([], 0)
    assert split_sentences._skip_checkpointed_rows(originals, 1) == ([{"id": 2}], 0)
    assert split_sentences._skip_checkpointed_rows(originals, 0) == (originals, 0)


def test_a_failure_removes_the_staged_file_and_keeps_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted run must leave the source shard and its durable prefix intact."""
    shard = _write(tmp_path / "region.parquet", [_row(0)])
    before = shard.read_bytes()

    def explode(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(split_sentences, "_promote_shard", explode)

    with pytest.raises(KeyboardInterrupt):
        segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=1)

    assert shard.read_bytes() == before
    assert not (tmp_path / ".region.parquet.segmenting").exists()
    assert (tmp_path / ".region.parquet.sentences.parts").is_dir()


def test_a_row_count_that_changed_under_the_run_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shard = _write(tmp_path / "region.parquet", [_row(0), _row(1)])
    monkeypatch.setattr(
        split_sentences, "_skip_checkpointed_rows", lambda originals, skip: ([], skip)
    )

    with pytest.raises(ValueError, match=rf"^{re.escape('sentence row count changed')}$"):
        segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=2)


def test_the_schema_error_names_the_offending_shard(tmp_path: Path) -> None:
    """The shard name is what makes the failure actionable in a multi-shard run."""
    row = {name: value for name, value in _row(0).items() if "language" not in name}
    row["schema_version"] = "v1.3"
    shard = _write(tmp_path / "odd-name.parquet", [row], schema=POLYGON_PUBLIC_SCHEMA)

    with pytest.raises(
        ValueError,
        match=rf"^{re.escape('unsupported polygon schema for segmentation: odd-name.parquet')}$",
    ):
        shard_needs_sentence_segmentation(shard)


def test_a_clock_exactly_on_the_deadline_stops(tmp_path: Path) -> None:
    """The budget is spent at the deadline, not one tick after it."""
    shard = _write(tmp_path / "region.parquet", [_row(0), _row(1)])
    ticks = iter([0.0, 10.0])

    result = segment_sentence_shard(
        shard,
        splitter=_FakeSplitter(),
        batch_rows=1,
        time_budget_seconds=10.0,
        clock=lambda: next(ticks, 10.0),
    )

    assert result.completed is False
    assert result.processed_rows == 0


def test_part_indices_advance_so_three_batches_produce_three_parts(tmp_path: Path) -> None:
    """A stalled or rewound index would collide with an existing part."""
    shard = _write(tmp_path / "region.parquet", [_row(0), _row(1), _row(2)])

    result = segment_sentence_shard(shard, splitter=_FakeSplitter(), batch_rows=1)

    assert result.completed is True
    assert [row["website_sentence_count"] for row in pq.read_table(shard).to_pylist()] == [2, 2, 2]


def test_a_paused_checkpoint_records_the_real_source_hash(tmp_path: Path) -> None:
    """Resuming depends on the stored hash actually identifying the source."""
    shard = _write(tmp_path / "region.parquet", [_row(0), _row(1)])
    expected = hash_shard(shard)
    ticks = iter([0.0, 0.0, 99.0])

    segment_sentence_shard(
        shard,
        splitter=_FakeSplitter(),
        batch_rows=1,
        time_budget_seconds=10.0,
        clock=lambda: next(ticks, 99.0),
    )

    metadata = json.loads(
        (tmp_path / ".region.parquet.sentences.parts" / "checkpoint.json").read_text()
    )
    assert metadata["source_shard_sha256"] == expected
    assert metadata["source_row_count"] == 2
