"""Tests for build_card."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from tests.fixtures.card import (
    _golden_card_stats,
    _language_card_stats,
    _public_row,
    _sentence_card_stats,
    _setup_minimal_run,
)

import osm_polygon_website_tag.reporting.card as card_module
import osm_polygon_website_tag.reporting.card_metadata as card_metadata
import osm_polygon_website_tag.reporting.card_rendering as card_rendering
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.reporting.card import (
    build_card,
)
from osm_polygon_website_tag.reporting.card_stats import compute_card_stats
from osm_polygon_website_tag.reporting.geometry_stats import (
    GeometryStats,
)


def test_card_stats_preserves_status_buckets_and_pending_semantics(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    rows = []
    statuses = (
        ("success", 3, "success", 2),
        ("empty", None, "unsafe_url", None),
        ("fetch_error", None, "pending", None),
        ("absent", None, "absent", None),
    )
    for index, (website_status, website_words, contact_status, contact_words) in enumerate(
        statuses
    ):
        row = _public_row(polygon_id=f"p{index}")
        row.update(
            {
                "website": None if website_status == "absent" else "https://example.com",
                "contact_website": None
                if contact_status == "absent"
                else "https://contact.example",
                "schema_version": "v1.3",
                "osm_id": 100 + index,
                "website_text": "text" if website_status == "success" else None,
                "website_word_count": website_words,
                "website_text_status": website_status,
                "contact_website_text": "text" if contact_status == "success" else None,
                "contact_website_word_count": contact_words,
                "contact_website_text_status": contact_status,
            }
        )
        rows.append(row)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "monaco-latest.parquet",
    )

    stats = compute_card_stats(run_dir)

    assert stats.website_urls_present == 3
    assert stats.contact_website_urls_present == 3
    assert stats.website_text_success_count == 1
    assert stats.contact_website_text_success_count == 1
    assert stats.website_text_empty_count == 1
    assert stats.contact_website_text_empty_count == 0
    assert stats.website_text_failure_count == 1
    assert stats.contact_website_text_failure_count == 1
    assert stats.website_total_words == 3
    assert stats.contact_website_total_words == 2
    assert stats.polygons_with_any_text == 1
    assert stats.enriched_sources_count == 0


def test_card_stats_does_not_mark_retryable_statuses_enriched(tmp_path: Path) -> None:
    """A failed or empty URL result keeps the source incomplete for the card."""
    run_dir = _setup_minimal_run(tmp_path)
    rows = []
    for index, (website_status, contact_status) in enumerate(
        (("fetch_error", "absent"), ("absent", "unsafe_url"))
    ):
        row = _public_row(polygon_id=f"retryable-{index}")
        row.update(
            {
                "schema_version": "v1.3",
                "website": None if website_status == "absent" else "https://example.com",
                "contact_website": None
                if contact_status == "absent"
                else "https://contact.example",
                "website_text_status": website_status,
                "contact_website_text_status": contact_status,
            }
        )
        rows.append(row)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "monaco-latest.parquet",
    )

    stats = compute_card_stats(run_dir)

    assert stats.enriched_sources_count == 0
    assert "| Status | In progress |" in build_card(run_dir).read_text()


def test_card_stats_can_scope_to_uploaded_sources(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    second = _public_row(polygon_id="p2", source_pbf="france-latest.osm.pbf")
    pq.write_table(
        pa.Table.from_pylist([second], schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "france-latest.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA),
        run_dir / "analysis_observations" / "france-latest.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([], schema=REJECTION_SCHEMA),
        run_dir / "rejections" / "france-latest.parquet",
    )

    stats = compute_card_stats(run_dir, source_names={"monaco-latest.osm.pbf"})

    assert stats.sources_count == 1
    assert stats.public_row_count == 1
    assert stats.observation_count == 0


def test_incremental_card_renders_progress_and_text_statistics(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    (run_dir / "manifests" / "expected_sources.json").write_text(
        json.dumps(
            [
                {"filename": "monaco-latest.osm.pbf", "size_bytes": 1, "mtime_ns": 1},
                {"filename": "france-latest.osm.pbf", "size_bytes": 1, "mtime_ns": 1},
            ]
        )
    )
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

    content = build_card(run_dir).read_text()

    assert "dataset_status: in_progress" in content
    assert "| Regional sources | 1 / 2 |" in content
    assert "| `website` | 1 | 1 | 0 | 0 | 3 |" in content
    assert "3 words in total" in content
    assert "Trafilatura" in content
    assert "Unicode `\\w+`" in content


def test_hostname_renderer_caps_public_table_at_ten_rows() -> None:
    from osm_polygon_website_tag.reporting.card_rendering import (
        _render_hostnames,
    )

    rows = [
        {"website_hostname": f"host-{index}.example", "row_count": 20 - index}
        for index in range(11)
    ]

    rendered = _render_hostnames(
        "website",
        rows,
        hostname_key="website_hostname",
    )

    assert "host-4.example" in rendered
    assert "host-10.example" not in rendered


def test_language_front_matter_and_section_render_detected_labels() -> None:
    front_matter = card_module._render_yaml_front_matter(_language_card_stats())

    assert "language:\n  - eng\n  - deu\n" in front_matter
    assert "detected_language_count: 2" in front_matter
    assert "website_language_count: 10" in front_matter
    assert "contact_website_language_count: 15" in front_matter

    body = card_module.render_markdown(_language_card_stats(), geometry=GeometryStats())
    assert "## Languages" in body
    assert "| `eng_Latn` | 25 |" in body


def test_language_section_is_absent_without_detected_languages() -> None:
    front_matter = card_module._render_yaml_front_matter(_golden_card_stats())

    assert "language:" not in front_matter
    assert "## Languages" not in card_module.render_markdown(
        _golden_card_stats(), geometry=GeometryStats()
    )


def test_release_yaml_removes_stale_language_tags_when_detection_is_empty() -> None:
    document = "---\nlanguage:\n  - eng\ndetected_language_count: 1\nlicense: odbl\n---\n"

    updated = card_metadata._update_existing_release_yaml(document.encode(), _golden_card_stats())
    updated = updated.decode()

    assert "language:" not in updated
    assert "license: odbl" in updated


def test_sentence_front_matter_and_section_render_segmentation_totals() -> None:
    front_matter = card_module._render_yaml_front_matter(_sentence_card_stats())

    assert "sentence_count: 60" in front_matter
    assert "website_segmented_count: 8" in front_matter
    assert "contact_website_segmented_count: 4" in front_matter

    body = card_module.render_markdown(_sentence_card_stats(), geometry=GeometryStats())
    assert "## Sentences" in body
    assert "| Sentences | 60 |" in body
    assert "| Segmented `website` texts | 8 |" in body
    assert "| Segmented `contact:website` texts | 4 |" in body
    assert "| Eligible text units for splitting | 15 |" in body
    assert "| Split with a supported language | 12 |" in body
    assert "| Left unsplit: unsupported language | 3 |" in body
    assert "| Sentence-splitting coverage | 80.0% |" in body
    assert "| Unsupported-language share | 20.0% |" in body
    assert "`hrv_Latn` (2)" in body
    assert "Mean sentences per segmented text: **5.0**" in body


def test_sentence_section_is_absent_without_segmentation() -> None:
    front_matter = card_module._render_yaml_front_matter(_language_card_stats())

    assert "sentence_count:" not in front_matter
    assert "## Sentences" not in card_module.render_markdown(
        _language_card_stats(), geometry=GeometryStats()
    )


def test_sentence_section_has_a_stable_line_contract() -> None:
    assert card_rendering._render_sentence_section(_sentence_card_stats()) == [
        "## Sentences",
        "",
        (
            "Segmented with [SaT](https://huggingface.co/segment-any-text/sat-3l-sm), which "
            "covers 85 languages; anything else records `unsupported_language`. Segments do "
            "not rejoin into the source text -- use the `*_text` columns for that."
        ),
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        "| Sentences | 60 |",
        "| Segmented `website` texts | 8 |",
        "| Segmented `contact:website` texts | 4 |",
        "| Eligible text units for splitting | 15 |",
        "| Split with a supported language | 12 |",
        "| Left unsplit: unsupported language | 3 |",
        "| Sentence-splitting coverage | 80.0% |",
        "| Unsupported-language share | 20.0% |",
        "",
        "Mean sentences per segmented text: **5.0**",
        "",
        "Most common unsupported languages: `hrv_Latn` (2), `zho_Hani` (1).",
        "",
    ]


def test_sentence_metadata_has_a_stable_line_contract() -> None:
    assert card_metadata._sentence_metadata_lines(_sentence_card_stats()) == [
        "sentence_count: 60",
        "website_segmented_count: 8",
        "contact_website_segmented_count: 4",
    ]
    assert card_metadata._sentence_metadata_lines(_language_card_stats()) == []


def test_sentence_section_renders_unsupported_only_population_without_dividing_by_zero() -> None:
    stats = _language_card_stats()
    stats.total_sentence_count = 0
    stats.website_sentence_row_count = 0
    stats.contact_website_sentence_row_count = 0
    stats.sentence_split_eligible_count = 3
    stats.sentence_split_supported_count = 0
    stats.sentence_split_unsupported_count = 3
    stats.unsupported_language_row_count = 3

    section = card_rendering._render_sentence_section(stats)

    assert "| Sentence-splitting coverage | 0.0% |" in section
    assert "Mean sentences per segmented text: **n/a**" in section


def test_sentence_count_helpers_keep_explicit_values_and_use_distinct_fallbacks() -> None:
    stats = replace(
        _sentence_card_stats(),
        website_sentence_row_count=2,
        contact_website_sentence_row_count=3,
        sentence_split_supported_count=4,
        sentence_split_unsupported_count=6,
        unsupported_language_row_count=9,
        sentence_split_eligible_count=12,
    )

    assert card_rendering._sentence_counts(stats) == (5, 4, 6, 12)
    assert card_rendering._reported_count(2, 7) == 2
    assert card_rendering._unsupported_language_rows(stats) == "`hrv_Latn` (2), `zho_Hani` (1)"
    assert (
        card_rendering._unsupported_language_rows(
            replace(stats, top_unsupported_sentence_languages=[])
        )
        == "none reported"
    )


def test_sentence_count_helpers_use_fallbacks_when_explicit_counts_are_zero() -> None:
    stats = replace(
        _sentence_card_stats(),
        website_sentence_row_count=2,
        contact_website_sentence_row_count=3,
        sentence_split_supported_count=0,
        sentence_split_unsupported_count=0,
        unsupported_language_row_count=8,
        sentence_split_eligible_count=0,
    )

    assert card_rendering._sentence_counts(stats) == (5, 5, 8, 13)


def test_yaml_custom_hash_ignores_a_trailing_document_newline(tmp_path: Path) -> None:
    front_matter = "---\nlicense: odbl\nconfigs:\n  - config_name: default\n---"
    yaml_path = tmp_path / "dataset.yaml"
    readme_path = tmp_path / "README.md"
    yaml_path.write_text(front_matter, encoding="utf-8")
    readme_path.write_text(f"{front_matter}\n\n# Title\n", encoding="utf-8")

    assert card_module.yaml_custom_sha256(yaml_path) == card_module.yaml_custom_sha256(readme_path)
