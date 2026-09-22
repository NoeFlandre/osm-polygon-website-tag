"""Tests for build_card."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.card import (
    _public_row,
    _setup_minimal_run,
    _ts,
)

import osm_polygon_website_tag.reporting.card_stats as card_stats_module
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.reporting.card import (
    build_card,
)
from osm_polygon_website_tag.reporting.card_stats import CardStats, compute_card_stats
from osm_polygon_website_tag.runtime.run_state import load_run, upsert_run_metadata


def test_build_card_map_counts_only_polygons_with_extracted_text(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    rows = [_public_row(polygon_id="pending"), _public_row(polygon_id="success")]
    rows[0]["website_text_status"] = "pending"
    rows[1]["website_text_status"] = "success"
    rows[1]["website_text"] = "extracted text"
    rows[1]["website_word_count"] = 2
    pq.write_table(
        pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "monaco-latest.parquet",
    )

    build_card(run_dir)

    card = (run_dir / "README.md").read_text()
    assert "covering **1** unique polygons with extracted text" in card


def test_build_card_embeds_observation_count(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    path = build_card(run_dir)
    content = path.read_text()
    assert "| Polygons | 1 |" in content
    assert "| Regional sources | 1 / 1 |" in content
    assert "| Duplicate objects removed | 0 |" in content
    assert "`website` or `contact:website`" in content


def test_build_card_lists_language_columns_for_v1_4_runs(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    shard = run_dir / "polygons" / "monaco-latest.parquet"
    rows = pq.read_table(shard).to_pylist()
    rows[0].update(
        {
            "website_language": "eng_Latn",
            "website_language_probability": 0.9,
            "contact_website_language": None,
            "contact_website_language_probability": None,
        }
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA_V1_4), shard)

    content = build_card(run_dir).read_text()

    assert "`website_language`" in content
    assert "`website_language_probability`" in content
    assert "published polygon split is globally canonicalized" not in content
    assert "at most one row per OSM object" not in content
    assert "`deduplication_summary.json`" in content
    assert "| `polygon_id` |" in content
    assert "| `contact_website` |" in content
    assert "| `preferred_website` |" not in content
    assert "| `wikidata` |" not in content
    assert "| `area_km2` |" not in content


def test_build_card_renders_done_snapshot_without_zero_canonical_metric(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    state = load_run(run_dir)
    upsert_run_metadata(state, {"snapshot_status": "done"})

    content = build_card(run_dir).read_text()

    assert "dataset_status: done" in content
    assert "| Status | Done |" in content
    assert "| Canonical polygons |" not in content
    assert "canonical_count:" not in content
    assert "Regional sources" in content
    assert "| Regional sources |" in content
    assert "anything other than a public IP are refused as `unsafe_url`" in content
    assert "This snapshot is frozen" in content
    assert "Failed values retry on later resumptions" not in content


def test_build_card_links_detailed_analysis_instead_of_embedding_it(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    analyze_results(run_dir)
    path = build_card(run_dir)
    content = path.read_text()
    assert "Eight-cell provenance cube" not in content
    assert "Per-source coverage" not in content
    assert "`analysis/*.parquet`" in content


def test_card_stats_populates_canonical_count_from_analysis(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "osm_type": "way",
                    "osm_id": 100,
                    "osm_version": 1,
                    "osm_timestamp": _ts(),
                    "source_pbf": "monaco-latest.osm.pbf",
                    "region": "monaco",
                    "primary_category": "building",
                    "website": "https://example.com",
                    "contact_website": None,
                    "wikidata": None,
                    "has_website": True,
                    "has_contact_website": False,
                    "has_any_website": True,
                    "has_wikidata": False,
                    "schema_version": "v1.1",
                }
            ],
            schema=COMPARISON_OBSERVATION_SCHEMA,
        ),
        run_dir / "analysis_observations" / "monaco-latest.parquet",
    )
    analyze_results(run_dir)

    stats = compute_card_stats(run_dir)

    assert stats.canonical_count == 1


def test_card_stats_fails_closed_on_corrupt_parquet(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    (run_dir / "polygons" / "monaco-latest.parquet").write_bytes(b"corrupt")

    with pytest.raises(pa.ArrowInvalid):
        compute_card_stats(run_dir)


def test_build_card_is_idempotent(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    p1 = build_card(run_dir).read_text()
    p2 = build_card(run_dir).read_text()
    assert p1 == p2


@pytest.mark.parametrize(
    ("row_count", "expected"),
    [
        (0, "n<1K"),
        (999, "n<1K"),
        (1_000, "1K<n<10K"),
        (9_999, "1K<n<10K"),
        (10_000, "10K<n<100K"),
        (100_000, "100K<n<1M"),
        (1_000_000, "1M<n<10M"),
        (10_000_000, "10M<n<100M"),
        (100_000_000, "100M<n<1B"),
        (1_000_000_000, "n>1B"),
    ],
)
def test_size_category_is_derived_from_public_row_count(row_count: int, expected: str) -> None:
    from osm_polygon_website_tag.reporting.card import _size_category

    assert _size_category(row_count) == expected


def test_card_stats_derives_text_and_word_totals_from_polygon_parquet(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    row = _public_row()
    row.update(
        {
            "schema_version": "v1.2",
            "website_text": "one two three",
            "website_word_count": 3,
            "website_text_status": "success",
            "contact_website_text": None,
            "contact_website_word_count": None,
            "contact_website_text_status": "absent",
        }
    )
    pq.write_table(
        pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "monaco-latest.parquet",
    )

    stats = compute_card_stats(run_dir)

    assert stats.expected_sources_count == 1
    assert stats.enriched_sources_count == 1
    assert stats.website_urls_present == 1
    assert stats.website_text_success_count == 1
    assert stats.website_total_words == 3
    assert stats.contact_website_urls_present == 0
    assert stats.polygons_with_any_text == 1


def test_card_counts_unique_polygons_with_trimmed_successful_text_across_regions(
    tmp_path: Path,
) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    duplicate = _public_row(polygon_id="duplicate")
    duplicate.update(
        {
            "website_text": "  website text  ",
            "website_word_count": 2,
            "website_text_status": "success",
        }
    )
    duplicate_copy = _public_row(polygon_id="duplicate", source_pbf="france-latest.osm.pbf")
    duplicate_copy.update({"website_text": None, "website_text_status": "pending"})
    contact_only = _public_row(polygon_id="contact-only")
    contact_only.update(
        {
            "osm_id": 101,
            "website": None,
            "has_website": False,
            "contact_website": "https://contact.example",
            "has_contact_website": True,
            "contact_website_text": " contact text ",
            "contact_website_word_count": 2,
            "contact_website_text_status": "success",
        }
    )
    whitespace = _public_row(polygon_id="whitespace")
    whitespace.update(
        {
            "osm_id": 102,
            "website_text": " \t\n",
            "website_word_count": 0,
            "website_text_status": "success",
        }
    )
    unsuccessful = _public_row(polygon_id="unsuccessful")
    unsuccessful.update(
        {"osm_id": 103, "website_text": "not counted", "website_text_status": "fetch_error"}
    )
    for filename, rows in (
        ("monaco-latest.parquet", [duplicate, contact_only, whitespace, unsuccessful]),
        ("france-latest.parquet", [duplicate_copy]),
    ):
        pq.write_table(
            pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA),
            run_dir / "polygons" / filename,
        )

    stats = compute_card_stats(run_dir)

    assert stats.polygons_with_any_text == 2
    assert stats.public_row_count == 5
    assert stats.website_text_success_count == 1
    assert stats.contact_website_text_success_count == 1
    assert stats.website_text_failure_count == 1


def test_card_counts_text_from_regional_copies_when_run_is_canonical(
    tmp_path: Path,
) -> None:
    regional_run = _setup_minimal_run(tmp_path / "regional")
    regional_row = _public_row(polygon_id="regional-copy")
    regional_row.update(
        {
            "website_text": "regional text",
            "website_word_count": 2,
            "website_text_status": "success",
        }
    )
    pq.write_table(
        pa.Table.from_pylist([regional_row], schema=POLYGON_PUBLIC_SCHEMA),
        regional_run / "polygons" / "monaco-latest.parquet",
    )

    canonical_run = _setup_minimal_run(tmp_path / "canonical")
    (canonical_run / "analysis_observations" / "monaco-latest.parquet").unlink()
    (canonical_run / "analysis_observations").rmdir()
    (canonical_run / "analysis_observations").symlink_to(
        regional_run / "analysis_observations",
        target_is_directory=True,
    )

    stats = compute_card_stats(canonical_run)

    assert stats.public_row_count == 1
    assert stats.polygons_with_any_text == 1


def test_regional_public_shards_preserves_scope_and_path(tmp_path: Path) -> None:
    regional_run = _setup_minimal_run(tmp_path / "regional")
    france = _public_row(polygon_id="france-copy", source_pbf="france-latest.osm.pbf")
    pq.write_table(
        pa.Table.from_pylist([france], schema=POLYGON_PUBLIC_SCHEMA),
        regional_run / "polygons" / "france-latest.parquet",
    )

    canonical_run = _setup_minimal_run(tmp_path / "canonical")
    (canonical_run / "analysis_observations" / "monaco-latest.parquet").unlink()
    (canonical_run / "analysis_observations").rmdir()
    (canonical_run / "analysis_observations").symlink_to(
        regional_run / "analysis_observations",
        target_is_directory=True,
    )

    regional_shards = card_stats_module._regional_public_shards(
        [canonical_run / "polygons" / "monaco-latest.parquet"],
        [canonical_run / "analysis_observations" / "monaco-latest.parquet"],
        source_names={"france-latest.osm.pbf"},
    )

    assert regional_shards == [regional_run / "polygons" / "france-latest.parquet"]


def test_regional_public_shards_falls_back_without_observation_shards(tmp_path: Path) -> None:
    public_shards = [tmp_path / "polygons" / "monaco-latest.parquet"]

    assert (
        card_stats_module._regional_public_shards(
            public_shards,
            [],
            source_names=None,
        )
        == public_shards
    )


def test_regional_public_shards_falls_back_without_regional_polygons(
    tmp_path: Path,
) -> None:
    regional_observations = tmp_path / "regional" / "analysis_observations"
    regional_observations.mkdir(parents=True)
    canonical_observations = tmp_path / "canonical" / "analysis_observations"
    canonical_observations.parent.mkdir()
    canonical_observations.symlink_to(regional_observations, target_is_directory=True)
    public_shards = [tmp_path / "canonical" / "polygons" / "monaco-latest.parquet"]

    assert (
        card_stats_module._regional_public_shards(
            public_shards,
            [canonical_observations / "monaco-latest.parquet"],
            source_names=None,
        )
        == public_shards
    )


def test_card_explains_unique_polygon_text_metric(tmp_path: Path) -> None:
    content = build_card(_setup_minimal_run(tmp_path)).read_text()

    assert "| With extracted text | 0 |" in content
    assert "unique `(osm_type, osm_id)` polygons" in content


def test_text_polygon_ids_preserves_osm_identity_and_skips_null_ids() -> None:
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array(["way", "node", None, "relation", "way"]),
            pa.array([42, 42, 99, None, None], type=pa.int64()),
            pa.array(["way text", "node text", "null type", "null id", "null id"]),
            pa.array(["success"] * 5),
            pa.array([None] * 5, type=pa.large_string()),
            pa.array(["absent"] * 5),
        ],
        names=[
            "osm_type",
            "osm_id",
            "website_text",
            "website_text_status",
            "contact_website_text",
            "contact_website_text_status",
        ],
    )

    assert card_stats_module._text_polygon_ids(batch) == {("way", 42), ("node", 42)}


def test_text_polygon_ids_rejects_mismatched_filtered_identity_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array(["way"]),
            pa.array([42], type=pa.int64()),
            pa.array(["text"]),
            pa.array(["success"]),
            pa.array([None], type=pa.large_string()),
            pa.array(["absent"]),
        ],
        names=[
            "osm_type",
            "osm_id",
            "website_text",
            "website_text_status",
            "contact_website_text",
            "contact_website_text_status",
        ],
    )
    original_kernel = card_stats_module.call_arrow_kernel
    filter_calls = 0

    def mismatched_filter(name: str, *args: object) -> object:
        nonlocal filter_calls
        if name == "filter":
            filter_calls += 1
            return pa.array(["way"]) if filter_calls == 1 else pa.array([42, 43])
        return original_kernel(name, *args)

    monkeypatch.setattr(card_stats_module, "call_arrow_kernel", mismatched_filter)

    with pytest.raises(ValueError, match="longer than"):
        card_stats_module._text_polygon_ids(batch)


def test_non_empty_successful_text_mask_requires_trimmed_text_and_success() -> None:
    mask = card_stats_module._non_empty_successful_text_mask(
        pa.array(["  text  ", " \t\n", None, "failed text"]),
        pa.array(["success", "success", "success", "fetch_error"]),
    )

    assert mask.to_pylist() == [True, False, False, False]


def test_unique_polygon_text_count_uses_bounded_required_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.parquet"
    row = _public_row()
    row.update({"website_text": "text", "website_text_status": "success"})
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA), path)
    real_parquet = pq.ParquetFile(path)
    calls: list[dict[str, object]] = []

    class TrackedParquet:
        schema_arrow = real_parquet.schema_arrow

        def iter_batches(self, **kwargs: object):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            yield from real_parquet.iter_batches(**kwargs)

    monkeypatch.setattr(card_stats_module.pq, "ParquetFile", lambda _path: TrackedParquet())
    stats = CardStats()

    card_stats_module._set_unique_polygon_text_count(stats, [path])

    assert stats.polygons_with_any_text == 1
    assert calls == [
        {
            "columns": [
                "osm_type",
                "osm_id",
                "website_text",
                "website_text_status",
                "contact_website_text",
                "contact_website_text_status",
            ],
            "batch_size": 8_192,
        }
    ]
