"""Grid'5000 preparation, run, and sync CLI adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from osm_polygon_website_tag.pipeline.grid5000 import (
    DEFAULT_GRID_LANGUAGE_BATCH_ROWS,
    DEFAULT_GRID_TIME_BUDGET_SECONDS,
    prepare_language_bundle,
    run_language_bundle,
    sync_language_bundle,
)
from osm_polygon_website_tag.pipeline.grid5000_sentences import (
    DEFAULT_GRID_MAX_ROWS,
    DEFAULT_GRID_SENTENCE_BATCH_ROWS,
    load_sentence_bundle,
    prepare_sentence_bundle,
    run_sentence_bundle,
    sync_sentence_bundle,
)
from osm_polygon_website_tag.pipeline.sat import load_sat_splitter_from_path
from osm_polygon_website_tag.runtime.paths import require_under_data_root

from . import RunDir, _json


def grid5000_run_sentences_command(
    bundle_dir: Annotated[Path, typer.Option("--bundle-dir", help="Grid'5000 bundle directory.")],
    time_budget_seconds: Annotated[
        float | None,
        typer.Option("--time-budget-seconds", help="Optional override within the bundle limit."),
    ] = None,
    batch_rows: Annotated[
        int | None,
        typer.Option("--batch-rows", help="Optional override for checkpoint batch size."),
    ] = None,
    job_id: Annotated[
        str | None, typer.Option("--job-id", help="Scheduler job id recorded in the receipt.")
    ] = None,
) -> int:
    """Segment one staged sentence bundle on a reserved node, offline."""
    bundle = load_sentence_bundle(bundle_dir)
    splitter = load_sat_splitter_from_path(
        Path(bundle_dir) / bundle.model.filename,
        revision=bundle.model.revision,
    )
    result = run_sentence_bundle(
        bundle_dir,
        splitter=splitter,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
        job_id=job_id,
    )
    _json(result.payload(), sort_keys=True)
    return 0


def grid5000_prepare_command(
    run_dir: RunDir,
    bundle_dir: Annotated[Path, typer.Option("--bundle-dir", help="Grid'5000 bundle directory.")],
    model_path: Annotated[
        Path, typer.Option("--model-path", help="Verified pinned GlotLID model binary.")
    ],
    commit: Annotated[
        str, typer.Option("--commit", help="Repository commit recorded in the bundle.")
    ],
    shard: Annotated[
        str | None,
        typer.Option("--shard", help="Optional source shard basename to stage."),
    ] = None,
    time_budget_seconds: Annotated[
        int,
        typer.Option("--time-budget-seconds", help="Detection budget within the 30-minute job."),
    ] = DEFAULT_GRID_TIME_BUDGET_SECONDS,
    batch_rows: Annotated[
        int,
        typer.Option("--batch-rows", help="Rows processed per language checkpoint batch."),
    ] = DEFAULT_GRID_LANGUAGE_BATCH_ROWS,
) -> int:
    """Prepare one data-root, offline Grid'5000 language bundle."""
    normalized_run_dir = require_under_data_root(run_dir, label="run directory")
    normalized_bundle_dir = require_under_data_root(bundle_dir, label="Grid'5000 bundle directory")
    normalized_model_path = require_under_data_root(model_path, label="GlotLID model path")
    bundle = prepare_language_bundle(
        normalized_run_dir,
        normalized_bundle_dir,
        model_path=normalized_model_path,
        commit=commit,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
        shard_name=shard,
    )
    _json({"bundle_dir": str(normalized_bundle_dir), **bundle.payload()}, sort_keys=True)
    return 0


def grid5000_run_command(
    bundle_dir: Annotated[Path, typer.Option("--bundle-dir", help="Grid'5000 bundle directory.")],
    time_budget_seconds: Annotated[
        float | None,
        typer.Option("--time-budget-seconds", help="Optional override within the bundle limit."),
    ] = None,
    batch_rows: Annotated[
        int | None,
        typer.Option("--batch-rows", help="Optional override for checkpoint batch size."),
    ] = None,
    job_id: Annotated[
        str | None, typer.Option("--job-id", help="Scheduler job id recorded in the receipt.")
    ] = None,
) -> int:
    """Run one staged bundle on a reserved node without network access."""
    result = run_language_bundle(
        bundle_dir,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
        job_id=job_id,
    )
    _json(result.payload(), sort_keys=True)
    return 0


def grid5000_prepare_sentences_command(
    run_dir: RunDir,
    bundle_dir: Annotated[Path, typer.Option("--bundle-dir", help="Grid'5000 bundle directory.")],
    model_dir: Annotated[
        Path, typer.Option("--model-dir", help="Locally staged SaT model directory.")
    ],
    model_revision: Annotated[
        str, typer.Option("--model-revision", help="Pinned Hugging Face revision of the SaT model.")
    ],
    commit: Annotated[
        str, typer.Option("--commit", help="Repository commit recorded in the bundle.")
    ],
    time_budget_seconds: Annotated[
        int,
        typer.Option("--time-budget-seconds", help="Segmentation budget within the job."),
    ] = DEFAULT_GRID_TIME_BUDGET_SECONDS,
    batch_rows: Annotated[
        int,
        typer.Option("--batch-rows", help="Rows processed per sentence checkpoint batch."),
    ] = DEFAULT_GRID_SENTENCE_BATCH_ROWS,
    max_rows: Annotated[
        int,
        typer.Option("--max-rows", help="Row budget packed into one bundle."),
    ] = DEFAULT_GRID_MAX_ROWS,
) -> int:
    """Prepare one data-root, offline Grid'5000 sentence bundle."""
    normalized_run_dir = require_under_data_root(run_dir, label="run directory")
    normalized_bundle_dir = require_under_data_root(bundle_dir, label="Grid'5000 bundle directory")
    normalized_model_dir = require_under_data_root(model_dir, label="SaT model directory")
    bundle = prepare_sentence_bundle(
        normalized_run_dir,
        normalized_bundle_dir,
        model_dir=normalized_model_dir,
        model_revision=model_revision,
        commit=commit,
        time_budget_seconds=time_budget_seconds,
        batch_rows=batch_rows,
        max_rows=max_rows,
    )
    _json({"bundle_dir": str(normalized_bundle_dir), **bundle.payload()}, sort_keys=True)
    return 0


def grid5000_sync_command(
    bundle_dir: Annotated[Path, typer.Option("--bundle-dir", help="Grid'5000 bundle directory.")],
    run_dir: RunDir,
) -> int:
    """Synchronize one Grid'5000 result into the canonical run."""
    normalized_bundle_dir = require_under_data_root(bundle_dir, label="Grid'5000 bundle directory")
    normalized_run_dir = require_under_data_root(run_dir, label="run directory")
    result = sync_language_bundle(normalized_bundle_dir, normalized_run_dir)
    _json(
        {
            "bundle_dir": str(normalized_bundle_dir),
            "run_dir": str(normalized_run_dir),
            **result.payload(),
        },
        sort_keys=True,
    )
    return 0


def grid5000_sync_sentences_command(
    bundle_dir: Annotated[Path, typer.Option("--bundle-dir", help="Grid'5000 bundle directory.")],
    run_dir: RunDir,
) -> int:
    """Synchronize one sentence receipt into the canonical run."""
    normalized_bundle_dir = require_under_data_root(bundle_dir, label="Grid'5000 bundle directory")
    normalized_run_dir = require_under_data_root(run_dir, label="run directory")
    result = sync_sentence_bundle(normalized_bundle_dir, normalized_run_dir)
    _json(
        {
            "bundle_dir": str(normalized_bundle_dir),
            "run_dir": str(normalized_run_dir),
            **result.payload(),
        },
        sort_keys=True,
    )
    return 0
