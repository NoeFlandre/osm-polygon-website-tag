"""Tests for deterministic, resumable source scheduling."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.application.resume_planner import (
    prepare_resume_priorities,
    prioritize_sources,
    summarize_enrichment_status,
)
from osm_polygon_website_tag.runtime.run_state import (
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
)


def test_prioritize_sources_puts_unprocessed_sources_first() -> None:
    sources = [Path("alsace-latest.osm.pbf"), Path("new-region.osm.pbf")]

    ordered = prioritize_sources(sources, {"alsace-latest.osm.pbf"})

    assert [source.name for source in ordered] == [
        "new-region.osm.pbf",
        "alsace-latest.osm.pbf",
    ]


def test_prioritize_sources_puts_unuploaded_before_retryable_sources() -> None:
    sources = [
        Path("uploaded-retry.osm.pbf"),
        Path("unuploaded.osm.pbf"),
        Path("uploaded-complete.osm.pbf"),
    ]

    ordered = prioritize_sources(
        sources,
        {"uploaded-complete.osm.pbf"},
        retry_names={"uploaded-retry.osm.pbf"},
    )

    assert [source.name for source in ordered] == [
        "unuploaded.osm.pbf",
        "uploaded-retry.osm.pbf",
        "uploaded-complete.osm.pbf",
    ]


def test_prioritize_sources_puts_partial_and_recoverable_work_first() -> None:
    sources = [
        Path("permanent-failure.osm.pbf"),
        Path("recoverable-failure.osm.pbf"),
        Path("partial.osm.pbf"),
        Path("complete.osm.pbf"),
        Path("untouched.osm.pbf"),
    ]

    ordered = prioritize_sources(
        sources,
        {"complete.osm.pbf"},
        retry_names={
            "permanent-failure.osm.pbf",
            "recoverable-failure.osm.pbf",
            "partial.osm.pbf",
        },
        partial_names={"partial.osm.pbf"},
        retry_priorities={
            "permanent-failure.osm.pbf": (3, 1),
            "recoverable-failure.osm.pbf": (1, 4),
            "partial.osm.pbf": (2, 2),
        },
    )

    assert [source.name for source in ordered] == [
        "untouched.osm.pbf",
        "partial.osm.pbf",
        "recoverable-failure.osm.pbf",
        "permanent-failure.osm.pbf",
        "complete.osm.pbf",
    ]


def test_summarize_enrichment_status_counts_each_text_field() -> None:
    summary = summarize_enrichment_status(
        pa.Table.from_pydict(
            {
                "website_text_status": pa.array(["success", "fetch_error", None]),
                "contact_website_text_status": pa.array(["absent", "unsafe_url", "success"]),
            }
        )
    )

    assert summary == {
        "website": {"__null__": 1, "fetch_error": 1, "success": 1},
        "contact_website": {"absent": 1, "success": 1, "unsafe_url": 1},
    }


def test_prepare_resume_priorities_backfills_legacy_status_summaries(tmp_path: Path) -> None:
    run_dir, state = initialise_run(tmp_path / "runs", run_id="production")
    source = tmp_path / "sources" / "recoverable.osm.pbf"
    source.parent.mkdir()
    source.write_bytes(b"source")
    fingerprint = snapshot_source_fingerprint(source)
    record_processed_source(state, fingerprint, public_row_count=2)
    shard = run_dir / "polygons" / "recoverable.parquet"
    shard.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pydict(
            {
                "website_text_status": pa.array(["fetch_error", "success"]),
                "contact_website_text_status": pa.array(["absent", "success"]),
            }
        ),
        shard,
    )

    partial_dir = run_dir / "polygons" / ".recoverable.parquet.enriching.parts"
    partial_dir.mkdir()
    (partial_dir / "checkpoint.json").write_text("{}")
    (partial_dir / "part-00000000.parquet").write_bytes(b"part")

    partial_names, priorities = prepare_resume_priorities(
        run_dir,
        state,
        [source],
        retry_names={source.name},
    )

    assert partial_names == {source.name}
    assert priorities[source.name] == (1, -4)
    persisted = load_run(run_dir).sources[source.name]
    assert persisted["enrichment_status_counts"]["website"]["fetch_error"] == 1
