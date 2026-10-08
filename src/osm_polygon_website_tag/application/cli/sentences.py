"""Sentence-segmentation CLI adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from osm_polygon_website_tag.pipeline.sat import load_sat_splitter_from_path
from osm_polygon_website_tag.pipeline.sentence_run import run_sentence_shards
from osm_polygon_website_tag.pipeline.split_sentences import (
    DEFAULT_BATCH_ROWS as DEFAULT_SENTENCE_BATCH_ROWS,
)
from osm_polygon_website_tag.pipeline.split_sentences import (
    shard_needs_sentence_segmentation,
    validate_segmentation_options,
)
from osm_polygon_website_tag.runtime.paths import require_under_data_root
from osm_polygon_website_tag.runtime.run_state import load_run

from ._common import BatchRows, OptionalSeconds, RunDir, echo_json
from .languages import (
    _language_command_payload,
    _record_completed_language_shard,
    _reject_frozen_language_run,
    _validate_language_shard_membership,
)


def segment_sentences_command(
    run_dir: RunDir,
    model_dir: Annotated[
        Path,
        typer.Option("--model-dir", help="Locally staged SaT model directory."),
    ],
    model_revision: Annotated[
        str,
        typer.Option("--model-revision", help="Pinned Hugging Face revision of the SaT model."),
    ],
    batch_rows: BatchRows = DEFAULT_SENTENCE_BATCH_ROWS,
    time_budget_seconds: OptionalSeconds = None,
) -> int:
    """Segment website text into sentences for every language-complete shard."""
    validate_segmentation_options(batch_rows, time_budget_seconds)
    normalized_run_dir = require_under_data_root(run_dir, label="run directory")
    state = load_run(normalized_run_dir)
    paths = sorted((normalized_run_dir / "polygons").glob("*.parquet"))
    _validate_language_shard_membership(state, paths)
    _reject_frozen_language_run(state)
    needed = [path for path in paths if shard_needs_sentence_segmentation(path)]
    if not needed:
        echo_json(
            _language_command_payload(
                normalized_run_dir,
                changed_shards=0,
                completed=True,
                processed_rows=0,
                bounded=time_budget_seconds is not None,
            ),
            sort_keys=True,
        )
        return 0
    splitter = load_sat_splitter_from_path(
        require_under_data_root(model_dir, label="SaT model directory"),
        revision=model_revision,
    )
    progress = run_sentence_shards(
        needed,
        splitter=splitter,
        record=lambda shard, result: _record_completed_language_shard(state, shard, result),
        batch_rows=batch_rows,
        time_budget_seconds=time_budget_seconds,
    )
    echo_json(
        _language_command_payload(
            normalized_run_dir,
            changed_shards=progress.changed_shards,
            completed=progress.completed,
            processed_rows=progress.processed_rows,
            bounded=time_budget_seconds is not None,
        ),
        sort_keys=True,
    )
    return 0
