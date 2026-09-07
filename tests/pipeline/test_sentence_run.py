"""Bounded multi-shard orchestration for the sentence stage."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity
from osm_polygon_website_tag.pipeline.sentence_run import (
    SentenceRunProgress,
    run_sentence_shards,
)
from osm_polygon_website_tag.pipeline.split_sentences import SentenceSegmentationResult


class _Splitter:
    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity("repo", "sat-3l-sm", "rev", "a" * 64)

    def split(self, texts: Sequence[str]) -> list[list[str]]:
        return [[text] for text in texts]


def _result(shard: Path, *, completed: bool = True, changed: bool = True) -> object:
    return SentenceSegmentationResult(
        shard_path=shard,
        row_count=3,
        changed=changed,
        shard_sha256="b" * 64,
        max_batch_rows=1,
        processed_rows=3,
        completed=completed,
    )


def _run(shards, segment, *, budget=None, clock=None, recorded=None):
    return run_sentence_shards(
        shards,
        splitter=_Splitter(),
        record=(
            (lambda shard, result: recorded.append((shard, result)))
            if recorded is not None
            else (lambda _shard, _result: None)
        ),
        batch_rows=8,
        time_budget_seconds=budget,
        clock=clock or (lambda: 0.0),
        segment=segment,
    )


def test_every_shard_is_processed_and_recorded_in_order(tmp_path: Path) -> None:
    shards = [tmp_path / "a.parquet", tmp_path / "b.parquet"]
    seen: list[Path] = []
    recorded: list[tuple[Path, object]] = []

    def segment(shard: Path, **_kwargs: object) -> object:
        seen.append(shard)
        return _result(shard)

    progress = _run(shards, segment, recorded=recorded)

    assert seen == shards
    assert [shard for shard, _ in recorded] == shards
    assert progress == SentenceRunProgress(changed_shards=2, processed_rows=6, completed=True)


def test_an_unchanged_shard_is_not_counted_as_changed(tmp_path: Path) -> None:
    shards = [tmp_path / "a.parquet"]

    progress = _run(shards, lambda shard, **_k: _result(shard, changed=False))

    assert progress.changed_shards == 0
    assert progress.completed is True


def test_a_paused_shard_stops_the_run_without_recording_it(tmp_path: Path) -> None:
    shards = [tmp_path / "a.parquet", tmp_path / "b.parquet"]
    recorded: list[tuple[Path, object]] = []

    progress = _run(shards, lambda shard, **_k: _result(shard, completed=False), recorded=recorded)

    assert progress == SentenceRunProgress(changed_shards=0, processed_rows=3, completed=False)
    assert recorded == []


def test_an_exhausted_budget_stops_before_the_next_shard(tmp_path: Path) -> None:
    shards = [tmp_path / "a.parquet", tmp_path / "b.parquet"]
    seen: list[Path] = []
    ticks = iter([0.0, 0.0, 99.0])

    def segment(shard: Path, **_kwargs: object) -> object:
        seen.append(shard)
        return _result(shard)

    progress = _run(shards, segment, budget=10.0, clock=lambda: next(ticks, 99.0))

    assert seen == shards[:1]
    assert progress == SentenceRunProgress(changed_shards=1, processed_rows=3, completed=False)


def test_the_remaining_budget_is_shared_across_shards(tmp_path: Path) -> None:
    """Each shard receives what is left of one shared budget, not a fresh one."""
    shards = [tmp_path / "a.parquet", tmp_path / "b.parquet"]
    budgets: list[object] = []
    ticks = iter([0.0, 1.0, 3.0])

    def segment(shard: Path, **kwargs: object) -> object:
        budgets.append(kwargs["time_budget_seconds"])
        return _result(shard)

    _run(shards, segment, budget=10.0, clock=lambda: next(ticks, 9.0))

    assert budgets == [9.0, 7.0]


def test_an_unbounded_run_passes_no_budget_to_any_shard(tmp_path: Path) -> None:
    budgets: list[object] = []

    def segment(shard: Path, **kwargs: object) -> object:
        budgets.append(kwargs["time_budget_seconds"])
        return _result(shard)

    _run([tmp_path / "a.parquet"], segment)

    assert budgets == [None]


def test_a_budget_spent_exactly_stops_before_the_next_shard(tmp_path: Path) -> None:
    """No time left means none, not a negative remainder."""
    shards = [tmp_path / "a.parquet", tmp_path / "b.parquet"]
    seen: list[Path] = []
    ticks = iter([0.0, 0.0, 10.0])

    def segment(shard: Path, **_kwargs: object) -> object:
        seen.append(shard)
        return _result(shard)

    progress = _run(shards, segment, budget=10.0, clock=lambda: next(ticks, 10.0))

    assert seen == shards[:1]
    assert progress.completed is False


def test_a_fraction_of_a_second_left_still_starts_the_next_shard(tmp_path: Path) -> None:
    """Only a spent budget stops the run, not merely a small one."""
    shards = [tmp_path / "a.parquet", tmp_path / "b.parquet"]
    seen: list[Path] = []
    ticks = iter([0.0, 0.0, 9.5])

    def segment(shard: Path, **_kwargs: object) -> object:
        seen.append(shard)
        return _result(shard)

    progress = _run(shards, segment, budget=10.0, clock=lambda: next(ticks, 9.5))

    assert seen == shards
    assert progress.completed is True
