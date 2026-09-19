"""Tests for build_card."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.card import (
    _golden_card_stats,
    _golden_geometry_stats,
    _language_card_stats,
    _setup_minimal_run,
)

import osm_polygon_website_tag.reporting.card as card_module
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.reporting.card import (
    _append_geometry_block,
    _geometry_block_bytes,
    _render_bbox,
    _render_language_section,
    _render_polygon_geometry_section,
    _update_geometry_section,
    _update_language_section,
    _update_website_text_section,
    build_card,
    update_card_with_geometry,
)
from osm_polygon_website_tag.reporting.card_stats import CardStats
from osm_polygon_website_tag.reporting.geometry_stats import (
    GeometryStats,
    render_geometry_stats,
)


def test_geometry_report_is_staged_only_when_its_bytes_change(tmp_path: Path) -> None:
    geometry = GeometryStats(row_count=7)
    staged = tmp_path / ".stats.json.building"
    target = tmp_path / "stats.json"

    assert card_module._staged_geometry_stats(staged, tmp_path, geometry) == [(staged, target)]
    assert staged.read_text(encoding="utf-8") == render_geometry_stats(geometry)

    target.write_text(render_geometry_stats(geometry), encoding="utf-8")
    staged.unlink()

    assert card_module._staged_geometry_stats(staged, tmp_path, geometry) == []
    assert not staged.exists()

    target.write_text("stale\n", encoding="utf-8")

    assert card_module._staged_geometry_stats(staged, tmp_path, geometry) == [(staged, target)]


def test_geometry_renderers_have_stable_numeric_and_newline_contracts() -> None:
    geometry = _golden_geometry_stats()
    assert _render_bbox(None) == "none"
    assert _render_bbox([-1.0, 2.0, 3.1234567, 4.0]) == "[-1.000000, 2.000000, 3.123457, 4.000000]"
    assert _render_polygon_geometry_section(geometry) == [
        "## Polygon geometry",
        "",
        (
            "Surface and shape statistics computed over every published polygon row from the "
            "`area_m2`, `bbox`, and `geometry` columns. Areas are geodesic on the WGS84 "
            "ellipsoid. Population scope: published polygon rows. The complete breakdown is published as [`stats.json`](stats.json)."
        ),
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        "| Polygons measured | 25 |",
        "| Total area | 2.50 km² |",
        "| Median area | 29.00 m² |",
        "| Mean area | 28.00 m² |",
        "| Smallest / largest area | 26.00 / 27.00 m² |",
        "| p95 area | 30.00 m² |",
        "| MultiPolygon rows | 32 |",
        "| Rows with holes | 33 |",
        "| Rows below 1 m² | 31 |",
        "",
        "Dataset bounding box: `[-1.500000, -2.500000, 3.500000, 4.500000]` (min lon, min lat, max lon, max lat).",
        "",
    ]
    missing_p95 = replace(
        geometry,
        area=replace(geometry.area, summary=replace(geometry.area.summary, percentiles={})),
    )
    assert "| p95 area | 0.00 m² |" in _render_polygon_geometry_section(missing_p95)


def test_geometry_block_preserves_prefix_and_card_newline_conventions() -> None:
    geometry = GeometryStats(row_count=1)
    block = _geometry_block_bytes(geometry, b"\n")
    assert block.startswith(b"## Polygon geometry\n")
    assert block.endswith(b"\n")
    assert _append_geometry_block(b"prefix", b"BLOCK\n", b"\n") == b"prefix\n\nBLOCK\n"
    assert _append_geometry_block(b"prefix\n", b"BLOCK\n", b"\n") == b"prefix\n\nBLOCK\n"
    assert _append_geometry_block(b"", b"BLOCK\n", b"\n") == b"BLOCK\n"


def test_update_geometry_section_replaces_inserts_and_appends_without_touching_neighbors() -> None:
    geometry = GeometryStats(row_count=1)
    block = _geometry_block_bytes(geometry, b"\n")
    existing = _update_geometry_section(
        b"prefix\n## Polygon geometry\nold\n## Next\nkeep\n",
        geometry,
    )
    assert existing.startswith(b"prefix\n")
    assert existing.count(b"## Polygon geometry\n") == 1
    assert existing.endswith(b"## Next\nkeep\n")
    assert block in existing

    inserted = _update_geometry_section(
        b"prefix\n## Geographic distribution\nkeep\n",
        geometry,
    )
    assert inserted == b"prefix\n" + block + b"## Geographic distribution\nkeep\n"

    appended = _update_geometry_section(b"prefix", geometry)
    assert appended == b"prefix\n\n" + block

    crlf = _update_geometry_section(b"prefix\r\n## Geographic distribution\r\nkeep\r\n", geometry)
    assert b"## Polygon geometry\r\n" in crlf
    assert b"## Polygon geometry\n" not in crlf


def test_update_website_text_section_replaces_only_the_generated_block() -> None:
    stats = CardStats(
        website_urls_present=1,
        website_text_success_count=2,
        website_total_words=3,
        contact_website_urls_present=4,
        contact_website_text_success_count=5,
        contact_website_total_words=6,
        polygons_with_any_text=7,
    )

    updated = _update_website_text_section(
        b"prefix\n## Website text\nSTALE\n## Languages\nkeep\n", stats
    )

    assert updated.startswith(b"prefix\n## Website text\n")
    assert b"STALE" not in updated
    assert b"Unique polygons with extracted text: **7**" in updated
    assert updated.endswith(b"## Languages\nkeep\n")


def test_update_density_yaml_inserts_missing_fields_before_newline_terminated_closure() -> None:
    stats = CardStats(
        polygon_density_h3_resolution=3,
        polygon_density_row_count=7,
        occupied_h3_cell_count=5,
    )

    updated = card_module._update_density_yaml_text("license: odbl\n---\n", stats).decode()

    assert updated == (
        "license: odbl\n"
        "polygon_density_h3_resolution: 3\n"
        "polygon_density_row_count: 7\n"
        "occupied_h3_cell_count: 5\n"
        "---\n"
    )


def test_update_density_yaml_wrapper_handles_bytes_and_missing_documents() -> None:
    stats = CardStats(
        polygon_density_h3_resolution=3,
        polygon_density_row_count=7,
        occupied_h3_cell_count=5,
    )

    assert card_module._update_density_yaml(None, stats) == b""
    assert b"polygon_density_row_count: 7" in card_module._update_density_yaml(
        b"license: odbl\n---\n", stats
    )


def test_update_release_yaml_refreshes_global_text_fields_and_preserves_custom_fields() -> None:
    stats = CardStats(
        observation_count=1,
        public_row_count=2,
        rejection_count=3,
        duplicate_count=4,
        conflicting_snapshot_count=5,
        sources_count=6,
        expected_sources_count=7,
        enriched_sources_count=8,
        website_text_success_count=9,
        website_total_words=10,
        contact_website_text_success_count=11,
        contact_website_total_words=12,
        polygons_with_any_text=13,
        polygon_density_h3_resolution=14,
        polygon_density_row_count=15,
        occupied_h3_cell_count=16,
        detected_language_count=2,
        website_language_count=9,
        contact_website_language_count=4,
        top_languages=[("eng_Latn", 9), ("deu_Latn", 4)],
    )

    updated = card_module._update_release_yaml_text(
        "custom_field: keep\nlanguage:\n  - old\nwebsite_text_success_count: 99\n---\n", stats
    ).decode()

    assert "custom_field: keep\n" in updated
    assert "language:\n  - eng\n  - deu\n" in updated
    assert "  - old\n" not in updated
    assert "website_text_success_count: 9\n" in updated
    assert "unique_text_identity_count: 13\n" in updated
    assert "polygon_density_row_count: 15\n" in updated
    assert "website_text_success_count: 99" not in updated


def test_update_release_yaml_inserts_missing_language_before_metadata_fields() -> None:
    updated = card_module._update_release_yaml_text(
        "---\nlicense: odbl\nsize_categories:\n  - n<1K\n---\n",
        _language_card_stats(),
    ).decode()

    assert updated.startswith("---\nlicense: odbl\n")
    assert "language:\n  - eng\n  - deu\nsize_categories:\n" in updated


def test_update_language_section_replaces_only_the_generated_block() -> None:
    updated = _update_language_section(
        b"prefix\n## Website text\nkeep\n## Languages\nSTALE\n## Polygon geometry\nkeep\n",
        _language_card_stats(),
    )

    assert b"STALE" not in updated
    assert b"| `eng_Latn` | 25 |" in updated
    assert updated.startswith(b"prefix\n## Website text\nkeep\n")
    assert updated.endswith(b"## Polygon geometry\nkeep\n")


def test_update_language_section_inserts_missing_block_after_website_text() -> None:
    updated = _update_language_section(
        b"prefix\n## Website text\nkeep\n## Polygon geometry\nkeep\n",
        _language_card_stats(),
    )

    assert b"## Website text\nkeep\n## Languages\n" in updated
    assert updated.endswith(b"## Polygon geometry\nkeep\n")


def test_update_language_section_appends_missing_block_without_website_text() -> None:
    updated = _update_language_section(b"prefix\n", _language_card_stats())

    assert updated.startswith(b"prefix\n\n## Languages\n")
    assert b"| `eng_Latn` | 25 |" in updated


def test_language_section_has_an_exact_empty_and_detected_contract() -> None:
    assert _render_language_section(_golden_card_stats()) == []
    assert _render_language_section(_language_card_stats()) == [
        "## Languages",
        "",
        (
            "Detected with GlotLID v3 on successfully extracted text; labels are exact "
            "script-aware `language_Script` codes with a top-1 probability column."
        ),
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        "| Distinct languages | 2 |",
        "| Labeled `website` texts | 10 |",
        "| Labeled `contact:website` texts | 15 |",
        "",
        "Top 2 labels across both tags:",
        "",
        "| Language | Texts |",
        "| --- | ---: |",
        "| `eng_Latn` | 25 |",
        "| `deu_Latn` | 5 |",
        "",
    ]


def test_update_card_with_geometry_falls_back_to_full_builder_for_missing_readme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_names = {"source.osm.pbf"}
    calls: list[tuple[Path, Collection[str] | None]] = []
    expected = tmp_path / "README.md"
    monkeypatch.setattr(
        card_module,
        "build_card",
        lambda root, *, source_names: calls.append((root, source_names)) or expected,
    )

    assert update_card_with_geometry(tmp_path, source_names=source_names) == expected
    assert calls == [(tmp_path, source_names)]


def test_update_card_with_geometry_promotes_only_changed_staged_files_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readme = tmp_path / "README.md"
    readme.write_bytes(b"original")
    geometry = GeometryStats(row_count=3)
    source_names = {"source.osm.pbf"}
    calls: list[tuple[str, object]] = []
    promoted: list[list[tuple[Path, Path]]] = []

    monkeypatch.setattr(
        card_module,
        "compute_geometry_stats",
        lambda root, *, source_names: calls.append(("geometry", (root, source_names))) or geometry,
    )
    monkeypatch.setattr(
        card_module,
        "_update_geometry_section",
        lambda original, received: calls.append(("update", (original, received))) or b"updated",
    )

    def stage(staged: Path, root: Path, received: GeometryStats) -> list[tuple[Path, Path]]:
        calls.append(("stage", (staged, root, received)))
        staged.write_text("stats", encoding="utf-8")
        return [(staged, root / "stats.json")]

    monkeypatch.setattr(card_module, "_staged_geometry_stats", stage)
    monkeypatch.setattr(card_module, "atomic_promote_bundle", promoted.append)

    assert update_card_with_geometry(tmp_path, source_names=source_names) == readme
    staged_readme = tmp_path / ".README.md.geometry.building"
    staged_stats = tmp_path / ".stats.json.geometry.building"
    assert calls == [
        ("geometry", (tmp_path, source_names)),
        ("update", (b"original", geometry)),
        ("stage", (staged_stats, tmp_path, geometry)),
    ]
    assert promoted == [[(staged_readme, readme), (staged_stats, tmp_path / "stats.json")]]
    assert not staged_readme.exists()
    assert not staged_stats.exists()


def test_staged_geometry_stats_requires_explicit_utf8_for_existing_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = GeometryStats(row_count=7)
    staged = tmp_path / ".stats.json.building"
    target = tmp_path / "stats.json"
    rendered = render_geometry_stats(geometry)
    target.write_text(rendered, encoding="utf-8")
    encodings: list[str | None] = []
    original_read_text = Path.read_text

    def read_text(
        path: Path,
        *,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        encodings.append(encoding)
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert card_module._staged_geometry_stats(staged, tmp_path, geometry) == []
    assert encodings == ["utf-8"]
    assert not staged.exists()


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (CardStats(snapshot_status="done"), "done"),
        (CardStats(expected_sources_count=1, enriched_sources_count=1), "complete"),
        (CardStats(), "in_progress"),
    ],
)
def test_dataset_status_value_and_label_cover_all_states(stats: CardStats, expected: str) -> None:
    assert card_module._dataset_status_value(stats) == expected
    assert (
        card_module._dataset_status_label(stats)
        == {
            "done": "Done",
            "complete": "Complete",
            "in_progress": "In progress",
        }[expected]
    )


def test_enrichment_policy_covers_frozen_and_retryable_snapshots() -> None:
    assert card_module._enrichment_policy(CardStats()) == (
        "A source is enriched only when every status is `success` or `absent`. "
        "Failed values retry on later resumptions; successful values are cached."
    )
    assert card_module._enrichment_policy(CardStats(snapshot_status="done")) == (
        "A source is enriched only when every status is `success` or `absent`. "
        "This snapshot is frozen: failed values remain as recorded and are not "
        "retried. Successful values are cached."
    )


def test_render_hostnames_covers_empty_valid_and_invalid_rows() -> None:
    assert (
        card_module._render_hostnames("website", [], hostname_key="website_hostname")
        == "### Top `website` hostnames\n\n_No hostnames observed._"
    )
    assert card_module._render_hostnames(
        "website",
        [{"website_hostname": "example.org", "row_count": 1_234}],
        hostname_key="website_hostname",
    ) == (
        "### Top `website` hostnames\n\n"
        "| Hostname | Polygons |\n| --- | ---: |\n| `example.org` | 1,234 |"
    )
    with pytest.raises(ValueError) as missing_hostname:
        card_module._render_hostnames(
            "website",
            [{"website_hostname": None, "row_count": 1}],
            hostname_key="website_hostname",
        )
    assert str(missing_hostname.value) == "invalid hostname analysis row"
    with pytest.raises(ValueError) as invalid_count:
        card_module._render_hostnames(
            "website",
            [{"website_hostname": "example.org", "row_count": "1"}],
            hostname_key="website_hostname",
        )
    assert str(invalid_count.value) == "invalid hostname analysis row"


def test_schema_rows_and_selected_public_paths_are_deterministic(tmp_path: Path) -> None:
    schema = pa.schema([POLYGON_PUBLIC_SCHEMA.field("polygon_id")])
    assert card_module._schema_rows(schema) == [
        "| `polygon_id` | `string` | no | Deterministic source-scoped identifier of the form ``<source-stem>:<osm_type>/<osm_id>``. |"
    ]

    polygons = tmp_path / "polygons"
    polygons.mkdir()
    first = polygons / "a.parquet"
    second = polygons / "b.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), first)
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), second)
    assert card_module._selected_public_paths(tmp_path, None) == [first, second]
    assert card_module._selected_public_paths(tmp_path, {"b.osm.pbf"}) == [second]


def test_schema_rows_escapes_descriptions_and_marks_nullable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(card_module, "column_doc", lambda _name: " left   | right ")
    schema = pa.schema([pa.field("name", pa.string(), nullable=True)])

    assert card_module._schema_rows(schema) == ["| `name` | `string` | yes | left \\| right |"]


def test_public_schema_selection_respects_source_filter_and_metadata_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    language_shard = polygons / "a.parquet"
    legacy_shard = polygons / "b.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA_V1_4), language_shard)
    pq.write_table(pa.Table.from_pylist([], schema=POLYGON_PUBLIC_SCHEMA), legacy_shard)
    assert card_module._public_schema_for_card(tmp_path, {"b.osm.pbf"}) is POLYGON_PUBLIC_SCHEMA

    checks: list[bool | None] = []

    class Schema:
        def equals(self, _other: object, *, check_metadata: bool | None = None) -> bool:
            checks.append(check_metadata)
            return True

    monkeypatch.setattr(card_module.pq, "read_schema", lambda _path: Schema())
    assert card_module._has_schema([language_shard], POLYGON_PUBLIC_SCHEMA_V1_4)
    assert checks == [True]


def test_build_card_writes_h3_density_map_and_card_section(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)

    build_card(run_dir)

    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    card = (run_dir / "README.md").read_text()
    assert map_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "## Geographic distribution" in card
    assert "assets/geographic_polygon_density.png" in card
    assert "H3 resolution 3" in card
