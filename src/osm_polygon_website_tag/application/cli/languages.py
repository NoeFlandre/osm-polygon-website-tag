"""Language-detection CLI adapter and bounded run helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from osm_polygon_website_tag.pipeline.detect_languages import (
    DEFAULT_BATCH_ROWS,
    detect_language_shard,
    shard_needs_language_detection,
    validate_language_detection_options,
)
from osm_polygon_website_tag.pipeline.glotlid import load_glotlid_detector
from osm_polygon_website_tag.runtime.paths import model_cache_dir, require_under_data_root
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    RunState,
    load_run,
    transition_status,
    update_public_shard_metadata,
)

from ._common import BatchRows, OptionalSeconds, RunDir, echo_json


def _record_completed_language_shard(
    state: RunState,
    shard: Path,
    result: Any,
) -> None:
    """Persist one completed language shard's ordinary manifest metadata."""
    update_public_shard_metadata(
        state,
        filename=f"{shard.stem}.osm.pbf",
        row_count=result.row_count,
        shard_sha256=result.shard_sha256,
    )


def _finish_language_command_state(state: RunState) -> None:
    """Complete the language stage after every shard was promoted."""
    if state.metadata.get("status") == STATUS_ENRICHING:
        transition_status(state, STATUS_ENRICHED)


def _language_budget_exhausted(remaining_budget: float | None) -> bool:
    """Return whether no time remains for another shard."""
    if remaining_budget is None:
        return False
    return remaining_budget <= 0


def _reject_frozen_language_run(state: RunState) -> None:
    """Reject mutation of a snapshot explicitly frozen by the operator."""
    if (
        state.metadata.get("status") == STATUS_COMPLETE
        and state.metadata.get("snapshot_status") == "done"
    ):
        raise ValueError("cannot add languages to a frozen snapshot")


def _validate_language_shard_membership(state: RunState, paths: list[Path]) -> None:
    """Require every public shard to belong to the run's source manifest."""
    for path in paths:
        source_name = f"{path.stem}.osm.pbf"
        if source_name not in state.sources:
            raise ValueError(f"language shard is not in the source manifest: {path.name}")


def detect_languages_command(
    run_dir: RunDir,
    batch_rows: BatchRows = DEFAULT_BATCH_ROWS,
    time_budget_seconds: OptionalSeconds = None,
) -> int:
    """Detect GlotLID languages for every completed text shard."""
    validate_language_detection_options(batch_rows, time_budget_seconds)
    normalized_run_dir = require_under_data_root(run_dir, label="run directory")
    state = load_run(normalized_run_dir)
    paths = sorted((normalized_run_dir / "polygons").glob("*.parquet"))
    _validate_language_shard_membership(state, paths)
    _reject_frozen_language_run(state)
    needed = _needed_language_shards(paths)
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
    _prepare_language_command_state(state)
    model_cache = model_cache_dir("glotlid")
    detector = load_glotlid_detector(model_cache)
    progress = _run_language_shards(
        needed,
        detector=detector,
        state=state,
        batch_rows=batch_rows,
        time_budget_seconds=time_budget_seconds,
    )
    if progress.completed:
        _finish_language_command_state(state)
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


def _needed_language_shards(paths: list[Path]) -> list[Path]:
    """Return unfinished language shards in deterministic order."""
    return [path for path in paths if shard_needs_language_detection(path)]


@dataclass(frozen=True)
class _LanguageRunProgress:
    """Aggregate progress for one bounded language command."""

    changed_shards: int
    processed_rows: int
    completed: bool


def _prepare_language_command_state(state: RunState) -> None:
    """Enter the resumable language stage or reject an unsuitable run."""
    _reject_frozen_language_run(state)
    status = state.metadata.get("status")
    if status in {STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}:
        transition_status(state, STATUS_ENRICHING)
    elif status not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("detect-languages requires an extracted/enriched run")


def _remaining_language_budget(
    time_budget_seconds: float | None,
    *,
    started_at: float | None,
) -> float | None:
    """Return the remaining shared budget for the next shard."""
    if time_budget_seconds is None or started_at is None:
        return None
    return time_budget_seconds - (monotonic() - started_at)


def _run_language_shards(
    shards: list[Path],
    *,
    detector: Any,
    state: RunState,
    batch_rows: int,
    time_budget_seconds: float | None,
) -> _LanguageRunProgress:
    """Process sorted language shards until complete or the shared budget expires."""
    changed_shards = 0
    processed_rows = 0
    started_at = _language_start_time(time_budget_seconds)
    for shard in shards:
        remaining_budget = _remaining_language_budget(
            time_budget_seconds,
            started_at=started_at,
        )
        if _language_budget_exhausted(remaining_budget):
            return _LanguageRunProgress(changed_shards, processed_rows, completed=False)
        result = detect_language_shard(
            shard,
            detector=detector,
            batch_rows=batch_rows,
            time_budget_seconds=remaining_budget,
        )
        processed_rows += result.processed_rows
        if not result.completed:
            return _LanguageRunProgress(changed_shards, processed_rows, completed=False)
        _record_completed_language_shard(state, shard, result)
        changed_shards += int(result.changed)
    return _LanguageRunProgress(changed_shards, processed_rows, completed=True)


def _language_start_time(time_budget_seconds: float | None) -> float | None:
    """Start one shared monotonic clock only for bounded commands."""
    if time_budget_seconds is None:
        return None
    return monotonic()


def _language_command_payload(
    run_dir: Path,
    *,
    changed_shards: int,
    completed: bool,
    processed_rows: int,
    bounded: bool,
) -> dict[str, object]:
    """Build the legacy or bounded language-command JSON response."""
    payload: dict[str, object] = {"changed_shards": changed_shards, "run_dir": str(run_dir)}
    if bounded:
        payload.update({"completed": completed, "processed_rows": processed_rows})
    return payload
