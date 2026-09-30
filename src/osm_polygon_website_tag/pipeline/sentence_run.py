"""Bounded multi-shard orchestration for the sentence-segmentation stage.

One monotonic budget is shared across every shard so that a walltime-limited
job (a Grid5000 reservation, say) stops cleanly between or inside shards and
resumes from its durable checkpoints on the next invocation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol

from osm_polygon_website_tag.pipeline.sentences import SentenceSplitter
from osm_polygon_website_tag.pipeline.split_sentences import (
    SentenceSegmentationResult,
    segment_sentence_shard,
)
from osm_polygon_website_tag.pipeline.time_budget import (
    budget_exhausted,
    seconds_remaining,
    start_deadline,
)


class _Segmenter(Protocol):
    def __call__(
        self,
        shard_path: Path | str,
        *,
        splitter: SentenceSplitter,
        batch_rows: int = ...,
        time_budget_seconds: float | None = ...,
    ) -> SentenceSegmentationResult: ...


@dataclass(frozen=True)
class SentenceRunProgress:
    """Aggregate progress for one bounded sentence command."""

    changed_shards: int
    processed_rows: int
    completed: bool


def run_sentence_shards(
    shards: list[Path],
    *,
    splitter: SentenceSplitter,
    record: Callable[[Path, SentenceSegmentationResult], None],
    batch_rows: int,
    time_budget_seconds: float | None,
    clock: Callable[[], float] = monotonic,
    segment: _Segmenter = segment_sentence_shard,
) -> SentenceRunProgress:
    """Segment sorted shards until complete or the shared budget expires."""
    deadline = start_deadline(time_budget_seconds, clock)
    changed_shards = 0
    processed_rows = 0
    for shard in shards:
        remaining = seconds_remaining(deadline, clock)
        if budget_exhausted(remaining):
            return SentenceRunProgress(changed_shards, processed_rows, completed=False)
        result = segment(
            shard, splitter=splitter, batch_rows=batch_rows, time_budget_seconds=remaining
        )
        processed_rows += result.processed_rows
        if not result.completed:
            return SentenceRunProgress(changed_shards, processed_rows, completed=False)
        record(shard, result)
        changed_shards += int(result.changed)
    return SentenceRunProgress(changed_shards, processed_rows, completed=True)


__all__ = ["SentenceRunProgress", "run_sentence_shards"]
