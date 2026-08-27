"""Per-source extraction, enrichment, language detection, and publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from osm_polygon_website_tag.pipeline.glotlid import LanguageDetector
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
from osm_polygon_website_tag.runtime.run_state import RunState, SourceFingerprint


@dataclass(frozen=True)
class SourceProcessingContext:
    """Immutable dependencies shared by one source-processing phase."""

    run_dir: Path
    state: RunState
    repo_id: str
    apply: bool
    progress: Callable[[str], None] | None
    invocation_id: str
    upload_checkpoint: CheckpointV2
    area_workers: int | None
    max_in_flight_areas: int | None
    fetch_workers: int | None
    detect_languages: bool
    language_detector: LanguageDetector | None


@dataclass(frozen=True)
class SourcePhaseCounts:
    """Counts returned by one ordered source-processing phase."""

    extracted: int = 0
    reused: int = 0
    uploaded: int = 0


@dataclass(frozen=True)
class _SourceTransactionResult:
    extracted: bool
    reused: bool
    uploaded: bool


def process_sources(
    *,
    sources: Sequence[Path],
    ordered_sources: Sequence[Path],
    fingerprints_by_name: Mapping[str, SourceFingerprint],
    context: SourceProcessingContext,
    allow_extraction: bool,
) -> SourcePhaseCounts:
    """Process sources in the caller-provided order and aggregate phase counts."""
    counts = SourcePhaseCounts()
    for index, source in enumerate(ordered_sources, start=1):
        result = _process_source(
            source=source,
            fingerprint=fingerprints_by_name[source.name],
            context=context,
            index=index,
            total=len(sources),
            allow_extraction=allow_extraction,
        )
        counts = SourcePhaseCounts(
            extracted=counts.extracted + int(result.extracted),
            reused=counts.reused + int(result.reused),
            uploaded=counts.uploaded + int(result.uploaded),
        )
    return counts


def _process_source(
    *,
    source: Path,
    fingerprint: SourceFingerprint,
    context: SourceProcessingContext,
    index: int,
    total: int,
    allow_extraction: bool,
) -> _SourceTransactionResult:
    del source, fingerprint, context, index, total, allow_extraction
    raise NotImplementedError


__all__ = ["SourcePhaseCounts", "SourceProcessingContext", "process_sources"]
