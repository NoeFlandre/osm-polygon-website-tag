"""Tests for build_card."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.reporting.card as card_module
import osm_polygon_website_tag.reporting.card_stats as card_stats_module
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.pipeline.analyze import analyze_results
from osm_polygon_website_tag.reporting.card import (
    _append_geometry_block,
    _geometry_block_bytes,
    _render_bbox,
    _render_language_section,
    _render_polygon_geometry_section,
    _render_snapshot_section,
    _update_geometry_section,
    _update_language_section,
    _update_website_text_section,
    build_card,
    update_card_with_geometry,
)
from osm_polygon_website_tag.reporting.card_stats import CardStats, compute_card_stats
from osm_polygon_website_tag.reporting.geometry_stats import (
    AreaStats,
    ExtentStats,
    GeometryStats,
    NumericSummary,
    ShapeStats,
    render_geometry_stats,
)
from osm_polygon_website_tag.runtime.run_state import initialise_run, load_run, upsert_run_metadata


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def test_card_stats_private_arrow_helpers_count_invalid_values_and_select_sources(
    tmp_path: Path,
) -> None:
    assert (
        card_stats_module._count_invalid_statuses(pa.array(["success", "unknown", None, "absent"]))
        == 2
    )
    directory = tmp_path / "polygons"
    directory.mkdir()
    first = directory / "a.parquet"
    second = directory / "b.parquet"
    pq.write_table(pa.table({"value": [1]}), first)
    pq.write_table(pa.table({"value": [2]}), second)
    assert card_stats_module._selected_parquets(directory, None) == [first, second]
    assert card_stats_module._selected_parquets(directory, {"b.osm.pbf"}) == [second]


def _public_row(*, polygon_id: str = "p1", source_pbf: str = "monaco-latest.osm.pbf"):
    return {
        "polygon_id": polygon_id,
        "region": "monaco",
        "source_pbf": source_pbf,
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "website": "https://example.com",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "preferred_website": "https://example.com",
        "preferred_website_source": "website",
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.com",
        "contact_website_hostname": None,
        "wikidata": "Q42",
        "wikidata_qid": "Q42",
        "wikidata_class": "canonical_qid",
        "name": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": "building",
        "geometry": json.dumps({"type": "Polygon", "coordinates": []}),
        "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0.0,0.0,0.0,0.0]",
        "area_m2": 50.0,
        "area_km2": 5e-5,
        "area_bucket": "10-100m2",
        "centroid_kind": "lambert_azimuthal_equal_area",
        "schema_version": "v1.1",
        "website_text": None,
        "website_word_count": None,
        "website_text_status": "pending",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _setup_minimal_run(tmp_path: Path) -> Path:
    run_dir, _ = initialise_run(tmp_path, run_id="r")
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pub.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([_public_row()], schema=POLYGON_PUBLIC_SCHEMA),
        pub,
        compression="snappy",
    )
    obs = run_dir / "analysis_observations" / "monaco-latest.parquet"
    obs.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA),
        obs,
        compression="snappy",
    )
    rej = run_dir / "rejections" / "monaco-latest.parquet"
    rej.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([], schema=REJECTION_SCHEMA), rej, compression="snappy")
    return run_dir


def test_card_stats_uses_arrow_columns_without_row_dicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text statistics scan Arrow columns directly instead of materializing rows."""

    class FakeBatch:
        num_rows = 1

        def column(self, name: str) -> pa.Array:
            values = {
                "website": pa.array(["https://example.com"]),
                "website_text_status": pa.array(["success"]),
                "website_word_count": pa.array([3], type=pa.int64()),
                "contact_website": pa.array([None], type=pa.string()),
                "contact_website_text_status": pa.array(["absent"]),
                "contact_website_word_count": pa.array([None], type=pa.int64()),
            }
            return values[name]

        def to_pylist(self) -> list[dict[str, object]]:
            raise AssertionError("card stats must not materialize row dictionaries")

    class FakeParquet:
        schema_arrow = POLYGON_PUBLIC_SCHEMA

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield FakeBatch()

    monkeypatch.setattr(card_stats_module.pq, "ParquetFile", lambda _path: FakeParquet())
    stats = card_stats_module.CardStats()
    card_stats_module._add_text_stats(stats, tmp_path / "source.parquet")

    assert stats.website_urls_present == 1
    assert stats.website_text_success_count == 1
    assert stats.website_total_words == 3


def test_build_card_writes_readme_and_yaml(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    path = build_card(run_dir)
    assert path.exists()
    assert (run_dir / "dataset.yaml").exists()
    content = path.read_text()
    assert content.startswith("---")
    assert "license: odbl" in content
    assert "license_name:" not in content
    assert "task_categories:" not in content
    assert "task_categories:" not in (run_dir / "dataset.yaml").read_text()
    assert "size_categories:\n  - n<1K" in content
    assert "© OpenStreetMap contributors" in content
    assert "https://www.openstreetmap.org/copyright" in content
    assert "https://download.geofabrik.de/" in content
    assert "Live metrics: [Trackio dashboard]" in content
    assert "https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics" in content
    assert "Live metrics: [Trackio dashboard](https://huggingface.co/spaces/" in content
    assert "NoeFlandre/osm-polygon-website-tag-metrics);" in content
    assert ".hf.space" not in content
    assert (
        "[GitHub repository and README](https://github.com/NoeFlandre/osm-polygon-website-tag)"
        in content
    )
    assert "Website text is third-party content" in content
    assert "grants no additional reuse rights" in content
    assert "Check the source site's terms or license" in content
    assert "## Citation" in content
    assert "blob/main/CITATION.cff" in content
    assert "https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag" in content
    assert "assets/hero.png" in content
    assert content.index("assets/hero.png") < content.index("# OSM Polygon Website Dataset") + 200
    assert content.index("## Methodology and quality") < content.index("## Public polygon schema")
    assert "Top `website` hostnames" not in content
    assert "Top `contact:website` hostnames" not in content


def test_source_scoped_card_build_refreshes_existing_yaml(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    first_source = {"monaco-latest.osm.pbf"}
    build_card(run_dir, source_names=first_source)

    second = _public_row(polygon_id="p2", source_pbf="france-latest.osm.pbf")
    pq.write_table(
        pa.Table.from_pylist([second], schema=POLYGON_PUBLIC_SCHEMA),
        run_dir / "polygons" / "france-latest.parquet",
    )

    build_card(run_dir, source_names={*first_source, "france-latest.osm.pbf"})

    assert "public_row_count: 2" in (run_dir / "dataset.yaml").read_text(encoding="utf-8")


def test_snapshot_section_renders_its_metrics_as_markdown_rows() -> None:
    stats = CardStats(
        snapshot_status="done",
        sources_count=2,
        expected_sources_count=3,
        public_row_count=4,
        observation_count=5,
        duplicate_count=6,
        conflicting_snapshot_count=7,
        rejection_count=8,
    )

    assert _render_snapshot_section(stats) == [
        "## Snapshot",
        "",
        "| Metric | Value | What it means |",
        "| --- | ---: | --- |",
        "| Snapshot status | Done | Current published snapshot |",
        "| Regional PBFs included | 2 / 3 | Published source shards / expected source PBFs |",
        "| Published polygon rows | 4 | Rows in the public `polygons/` files |",
        "| Comparison observations | 5 | Source-level records with a website, contact:website, or Wikidata tag |",
        "| Duplicate OSM objects | 6 | Objects observed in more than one source snapshot |",
        "| Conflicting snapshot observations | 7 | Repeated observations whose tag values disagree with the selected version |",
        "| Rejected polygon candidates | 8 | Candidate objects that did not produce a usable polygon row |",
        "",
    ]


def _golden_geometry_stats() -> GeometryStats:
    return GeometryStats(
        row_count=25,
        area=AreaStats(
            summary=NumericSummary(
                row_count=25,
                total=2_500_000.0,
                minimum=26.0,
                maximum=27.0,
                mean=28.0,
                median=29.0,
                percentiles={"p95": 30.0},
            ),
            below_one_m2_row_count=31,
        ),
        shape=ShapeStats(multipolygon_row_count=32, with_holes_row_count=33),
        extent=ExtentStats(bbox=[-1.5, -2.5, 3.5, 4.5]),
    )


def _golden_card_stats() -> CardStats:
    return CardStats(
        snapshot_status="in_progress",
        observation_count=2,
        public_row_count=3,
        rejection_count=4,
        sources_count=5,
        expected_sources_count=6,
        duplicate_count=7,
        conflicting_snapshot_count=8,
        website_urls_present=9,
        website_text_success_count=10,
        website_text_empty_count=11,
        website_text_failure_count=12,
        website_total_words=13,
        contact_website_urls_present=14,
        contact_website_text_success_count=15,
        contact_website_text_empty_count=16,
        contact_website_text_failure_count=17,
        contact_website_total_words=18,
        polygons_with_any_text=19,
        polygon_density_h3_resolution=20,
        occupied_h3_cell_count=21,
        polygon_density_row_count=22,
        top_hostnames_website=[{"website_hostname": "example.org", "row_count": 23}],
        top_hostnames_contact_website=[
            {"contact_website_hostname": "contact.example", "row_count": 24}
        ],
    )


def test_render_markdown_has_a_stable_complete_output_contract() -> None:
    expected = dedent(
        r"""
        # OSM Polygon Website Dataset

        ![osm-polygon-website-tag hero banner](assets/hero.png)

        OpenStreetMap closed ways and polygon relations carrying a non-empty `website` OR `contact:website` tag, with full main-page text extracted using Trafilatura. Every statistic below is regenerated from the current upload-acknowledged Parquet artifacts.

        ## Snapshot

        | Metric | Value | What it means |
        | --- | ---: | --- |
        | Snapshot status | In progress | Current published snapshot |
        | Regional PBFs included | 5 / 6 | Published source shards / expected source PBFs |
        | Published polygon rows | 3 | Rows in the public `polygons/` files |
        | Comparison observations | 2 | Source-level records with a website, contact:website, or Wikidata tag |
        | Duplicate OSM objects | 7 | Objects observed in more than one source snapshot |
        | Conflicting snapshot observations | 8 | Repeated observations whose tag values disagree with the selected version |
        | Rejected polygon candidates | 4 | Candidate objects that did not produce a usable polygon row |

        ## Website text

        | Tag | URLs | Successful | Empty | Failed | Words |
        | --- | ---: | ---: | ---: | ---: | ---: |
        | `website` | 9 | 10 | 11 | 12 | 13 |
        | `contact:website` | 14 | 15 | 16 | 17 | 18 |

        Website-text table counts are unique `(osm_type, osm_id)` identities across regional rows; regional overlap duplicates are removed globally.

        Unique polygons with extracted text: **19**
        Counts unique `(osm_type, osm_id)` polygons across regional rows when any copy has successful, trimmed non-empty website or contact:website text; regional overlap duplicates removed globally.
        Combined extracted words: **31**

        ## Polygon geometry

        Surface and shape statistics computed over every published polygon row from the `area_m2`, `bbox`, and `geometry` columns. Areas are geodesic on the WGS84 ellipsoid. Population scope: published polygon rows. The complete breakdown is published as [`stats.json`](stats.json).

        | Metric | Value |
        | --- | ---: |
        | Polygons measured | 25 |
        | Total area | 2.50 km² |
        | Median area | 29.00 m² |
        | Mean area | 28.00 m² |
        | Smallest / largest area | 26.00 / 27.00 m² |
        | p95 area | 30.00 m² |
        | MultiPolygon rows | 32 |
        | Rows with holes | 33 |
        | Rows below 1 m² | 31 |

        Dataset bounding box: `[-1.500000, -2.500000, 3.500000, 4.500000]` (min lon, min lat, max lon, max lat).

        ## Geographic distribution

        ![H3 polygon density](assets/geographic_polygon_density.png)

        H3 resolution 20 contains **21** occupied cells across **22** unique polygons with successfully extracted, non-empty website or contact:website text, globally deduplicated by `(osm_type, osm_id)`; regional overlap duplicates removed globally. The color scale is logarithmic, counts are absolute, and a Natural Earth 1:110m land backdrop provides geographic context.

        ## Links

        Live metrics: [Trackio dashboard](https://huggingface.co/spaces/NoeFlandre/osm-polygon-website-tag-metrics); it shows this frozen dataset snapshot.
        Code and README: [GitHub repository and README](https://github.com/NoeFlandre/osm-polygon-website-tag).


        ### Top `website` hostnames

        | Hostname | Polygons |
        | --- | ---: |
        | `example.org` | 23 |

        ### Top `contact:website` hostnames

        | Hostname | Polygons |
        | --- | ---: |
        | `contact.example` | 24 |
        ## Methodology and quality

        Geometry is assembled with libosmium. Full main text is extracted independently for both website tags with Trafilatura and is not truncated. Word counts are Python Unicode `\w+` matches.

        Text statuses are `absent`, `pending`, `success`, `empty`, `invalid_url`, `unsafe_url`, `fetch_error`, or `extract_error`. A source is enriched only when every status is `success` or `absent`. Failed values retry on later resumptions; successful values are cached.

        A URL is marked `unsafe_url` when its hostname, or any redirect target, does not resolve exclusively to globally routable public IP addresses. Localhost, private, reserved, multicast, and unspecified targets are blocked. Unsupported schemes and URLs containing credentials are classified as `invalid_url`; redirect limits, timeouts, oversized responses, and unsupported content types are recorded as `fetch_error`.

        ## Dataset contents

        - `polygons/*.parquet`: the public polygon split, one shard per source PBF.
        - `analysis/*.parquet`: detailed overlap, provenance, hostname, duplicate, conflict, and per-source statistics.
        - `deduplication_summary.json`: counts and tag-conflict totals from the global canonicalization pass.
        - `stats.json`: complete machine-readable polygon geometry statistics.
        - `manifests/`: source inventory, upload checkpoints, and completion receipt.

        ## Public polygon schema

        | Column | Type | Nullable | Description |
        | --- | --- | :---: | --- |
        | `polygon_id` | `string` | no | Deterministic source-scoped identifier of the form ``<source-stem>:<osm_type>/<osm_id>``. |

        ## Provenance and license

        Source filename, byte size, and nanosecond modification time are recorded before processing. The completion receipt binds finalized artifacts by relative path, byte size, and SHA-256.

        The map backdrop uses Natural Earth 1:110m Admin-0 country geography, distributed in the source tree under its public-domain terms.

        © OpenStreetMap contributors. OpenStreetMap data is available under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/); see the [OpenStreetMap copyright and attribution page](https://www.openstreetmap.org/copyright). Regional PBF extracts are provided by [Geofabrik](https://download.geofabrik.de/).

        Website text is third-party content, separate from the OSM data, and is not covered by the ODbL. This dataset asserts no license for that text and grants no additional reuse rights: copyright and licensing conditions remain with each source website. Check the source site's terms or license before using or redistributing extracted text.

        ## Citation

        If you use this dataset, please cite it using the machine-readable metadata in [`CITATION.cff`](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag/blob/main/CITATION.cff). GitHub and the Hugging Face dataset page can then display the citation directly.

        > Flandre, Noé. *OSM Polygon Website Tag Dataset*. [Hugging Face dataset](https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag)
        """
    ).lstrip()

    expected = expected.replace(
        "Unique polygons with extracted text: **19**\n",
        "Unique polygons with extracted text: **19**  \n",
    )
    schema = pa.schema([POLYGON_PUBLIC_SCHEMA.field("polygon_id")])
    assert (
        card_module._render_markdown(
            _golden_card_stats(), geometry=_golden_geometry_stats(), schema=schema
        )
        == expected
    )


def test_render_yaml_front_matter_has_a_stable_output_contract() -> None:
    expected = (
        dedent(
            """
        ---
        license: odbl
        tags:
          - openstreetmap
          - osm
          - polygon
          - website
          - wikidata
          - geographic-data
        size_categories:
          - n<1K
        configs:
          - config_name: default
            data_files:
              - split: polygons
                path: polygons/*.parquet
        observation_count: 2
        public_row_count: 3
        rejection_count: 4
        duplicate_count: 7
        conflicting_snapshot_count: 8
        sources_count: 5
        expected_sources_count: 6
        enriched_sources_count: 0
        dataset_status: in_progress
        website_text_success_count: 10
        website_total_words: 13
        contact_website_text_success_count: 15
        contact_website_total_words: 18
        unique_text_identity_count: 19
        polygon_density_h3_resolution: 20
        polygon_density_row_count: 22
        occupied_h3_cell_count: 21
        ---
        """
        )
        .lstrip()
        .rstrip("\n")
    )

    assert card_module._render_yaml_front_matter(_golden_card_stats()) == expected


def test_build_card_preserves_collaborator_and_staging_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "card"
    run_dir.mkdir()
    source_names = {"monaco-latest.osm.pbf"}
    text_population = object()
    summary = object()
    stats = CardStats(public_row_count=3)
    geometry = GeometryStats(row_count=4)
    calls: list[tuple[str, object]] = []
    writes: list[tuple[Path, str, str | None]] = []
    mkdirs: list[tuple[Path, bool, bool]] = []
    schema = pa.schema([POLYGON_PUBLIC_SCHEMA.field("polygon_id")])

    def fake_summary(
        path: Path,
        *,
        source_names: Collection[str] | None,
        aggregation_mode: str,
    ) -> object:
        calls.append(("summary", (path, source_names, aggregation_mode)))
        return summary

    def fake_text_population(
        path: Path,
        *,
        source_names: Collection[str] | None,
    ) -> object:
        calls.append(("text-population", (path, source_names)))
        return text_population

    def fake_stats(
        path: Path,
        *,
        summary: object,
        text_population: object,
        source_names: Collection[str] | None,
    ) -> CardStats:
        calls.append(("stats", (path, summary, text_population, source_names)))
        return stats

    def fake_map(
        path: Path,
        *,
        summary: object,
        output_path: Path,
        source_names: Collection[str] | None,
        aggregation_mode: str,
    ) -> None:
        calls.append(("map", (path, summary, output_path, source_names, aggregation_mode)))

    def fake_geometry_stats(
        path: Path,
        *,
        text_population: object,
        source_names: Collection[str] | None,
    ) -> GeometryStats:
        calls.append(("geometry", (path, text_population, source_names)))
        return geometry

    def fake_render_markdown(
        rendered_stats: CardStats,
        *,
        geometry: GeometryStats,
        schema: pa.Schema,
    ) -> str:
        calls.append(("markdown", (rendered_stats, geometry, schema)))
        return "body"

    def fake_render_geometry(rendered_geometry: GeometryStats) -> str:
        calls.append(("geometry-json", rendered_geometry))
        return "stats"

    def fake_public_schema(path: Path, names: Collection[str] | None) -> pa.Schema:
        calls.append(("schema", (path, names)))
        return schema

    def fake_render_yaml(rendered_stats: CardStats) -> str:
        calls.append(("yaml", rendered_stats))
        return "front"

    def fake_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        **_: object,
    ) -> int:
        writes.append((path, data, encoding))
        return len(data)

    def fake_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del mode
        mkdirs.append((path, parents, exist_ok))

    promoted: list[list[tuple[Path, Path]]] = []

    monkeypatch.setattr(card_module, "compute_text_population_summary", fake_text_population)
    monkeypatch.setattr(card_module, "compute_polygon_density_summary", fake_summary)
    monkeypatch.setattr(card_module, "compute_card_stats", fake_stats)
    monkeypatch.setattr(card_module, "compute_geometry_stats", fake_geometry_stats)
    monkeypatch.setattr(card_module, "render_geometry_stats", fake_render_geometry)
    monkeypatch.setattr(card_module, "build_polygon_density_map", fake_map)
    monkeypatch.setattr(card_module, "_render_markdown", fake_render_markdown)
    monkeypatch.setattr(card_module, "_render_yaml_front_matter", fake_render_yaml)
    monkeypatch.setattr(card_module, "_public_schema_for_card", fake_public_schema)
    monkeypatch.setattr(Path, "write_text", fake_write_text)
    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(card_module, "atomic_promote_bundle", promoted.append)

    assert card_module.build_card(run_dir, source_names=source_names) == run_dir / "README.md"
    assert calls[0] == ("text-population", (run_dir, source_names))
    assert calls[1] == ("summary", (run_dir, source_names, "global_unique_text"))
    assert calls[2] == ("stats", (run_dir, summary, text_population, source_names))
    assert calls[3] == ("geometry", (run_dir, text_population, source_names))
    assert calls[4] == ("schema", (run_dir, source_names))
    assert calls[5] == ("markdown", (stats, geometry, schema))
    assert calls[6] == ("yaml", stats)
    assert calls[7] == (
        "map",
        (
            run_dir,
            summary,
            run_dir / ".assets" / "geographic_polygon_density.png.building",
            source_names,
            "global_unique_text",
        ),
    )
    assert writes == [
        (run_dir / ".README.md.building", "front\nbody", "utf-8"),
        (run_dir / ".dataset.yaml.building", "front", "utf-8"),
        (run_dir / ".stats.json.building", "stats", "utf-8"),
    ]
    assert mkdirs == [(run_dir / ".assets", True, True)]
    assert promoted == [
        [
            (
                run_dir / ".assets" / "geographic_polygon_density.png.building",
                run_dir / "assets" / "geographic_polygon_density.png",
            ),
            (run_dir / ".README.md.building", run_dir / "README.md"),
            (run_dir / ".dataset.yaml.building", run_dir / "dataset.yaml"),
            (run_dir / ".stats.json.building", run_dir / "stats.json"),
        ]
    ]


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
    assert "across **1** unique polygons with successfully extracted, non-empty" in card


def test_build_card_embeds_observation_count(tmp_path: Path) -> None:
    run_dir = _setup_minimal_run(tmp_path)
    path = build_card(run_dir)
    content = path.read_text()
    assert "| Published polygon rows | 1 | Rows in the public `polygons/` files |" in content
    assert (
        "| Regional PBFs included | 1 / 1 | Published source shards / expected source PBFs |"
        in content
    )
    assert "| Comparison observations | 0 |" in content
    assert "| What it means |" in content
    assert "`website` OR `contact:website`" in content


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
    assert "| Snapshot status | Done |" in content
    assert "| Canonical polygons |" not in content
    assert "canonical_count:" not in content
    assert "Regional PBFs included" in content
    assert "expected source PBFs" in content
    assert "globally routable public IP addresses" in content
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

    assert "Unique polygons with extracted text: **0**" in content
    assert "globally deduplicated by `(osm_type, osm_id)`" in content


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
    assert "| Snapshot status | In progress |" in build_card(run_dir).read_text()


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
    assert "| Regional PBFs included | 1 / 2 |" in content
    assert "| `website` | 1 | 1 | 0 | 0 | 3 |" in content
    assert "Combined extracted words: **3**" in content
    assert "Trafilatura" in content
    assert "Unicode `\\w+`" in content


def test_hostname_renderer_caps_public_table_at_ten_rows() -> None:
    from osm_polygon_website_tag.reporting.card import _render_hostnames

    rows = [
        {"website_hostname": f"host-{index}.example", "row_count": 20 - index}
        for index in range(11)
    ]

    rendered = _render_hostnames(
        "website",
        rows,
        hostname_key="website_hostname",
    )

    assert "host-9.example" in rendered
    assert "host-10.example" not in rendered


def _language_card_stats() -> CardStats:
    stats = _golden_card_stats()
    stats.detected_language_count = 2
    stats.website_language_count = 10
    stats.contact_website_language_count = 15
    stats.top_languages = [("eng_Latn", 25), ("deu_Latn", 5)]
    return stats


def test_language_front_matter_and_section_render_detected_labels() -> None:
    front_matter = card_module._render_yaml_front_matter(_language_card_stats())

    assert "language:\n  - eng\n  - deu\n" in front_matter
    assert "detected_language_count: 2" in front_matter
    assert "website_language_count: 10" in front_matter
    assert "contact_website_language_count: 15" in front_matter

    body = card_module._render_markdown(_language_card_stats(), geometry=GeometryStats())
    assert "## Languages" in body
    assert "| `eng_Latn` | 25 |" in body


def test_language_section_is_absent_without_detected_languages() -> None:
    front_matter = card_module._render_yaml_front_matter(_golden_card_stats())

    assert "language:" not in front_matter
    assert "## Languages" not in card_module._render_markdown(
        _golden_card_stats(), geometry=GeometryStats()
    )


def _sentence_card_stats() -> CardStats:
    stats = _language_card_stats()
    stats.total_sentence_count = 60
    stats.website_sentence_row_count = 8
    stats.contact_website_sentence_row_count = 4
    stats.unsupported_language_row_count = 3
    return stats


def test_sentence_front_matter_and_section_render_segmentation_totals() -> None:
    front_matter = card_module._render_yaml_front_matter(_sentence_card_stats())

    assert "sentence_count: 60" in front_matter
    assert "website_segmented_count: 8" in front_matter
    assert "contact_website_segmented_count: 4" in front_matter

    body = card_module._render_markdown(_sentence_card_stats(), geometry=GeometryStats())
    assert "## Sentences" in body
    assert "| Sentences | 60 |" in body
    assert "| Segmented `website` texts | 8 |" in body
    assert "| Segmented `contact:website` texts | 4 |" in body
    assert "| Texts in an uncovered language | 3 |" in body
    assert "Mean sentences per segmented text: **5.0**" in body


def test_sentence_section_is_absent_without_segmentation() -> None:
    front_matter = card_module._render_yaml_front_matter(_language_card_stats())

    assert "sentence_count:" not in front_matter
    assert "## Sentences" not in card_module._render_markdown(
        _language_card_stats(), geometry=GeometryStats()
    )


def test_sentence_section_has_a_stable_line_contract() -> None:
    assert card_module._render_sentence_section(_sentence_card_stats()) == [
        "## Sentences",
        "",
        (
            "Extracted text is segmented with "
            "[SaT](https://huggingface.co/segment-any-text/sat-3l-sm) for the 85 languages the "
            "segmenter covers; text in any other detected language records "
            "`unsupported_language` instead of sentences. Segments carry their own trailing "
            "spaces but not the line breaks that separated them, so joining them does not "
            "reproduce the source text; the full text stays in the `*_text` columns."
        ),
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        "| Sentences | 60 |",
        "| Segmented `website` texts | 8 |",
        "| Segmented `contact:website` texts | 4 |",
        "| Texts in an uncovered language | 3 |",
        "",
        "Mean sentences per segmented text: **5.0**",
        "",
    ]


def test_sentence_metadata_has_a_stable_line_contract() -> None:
    assert card_module._sentence_metadata_lines(_sentence_card_stats()) == [
        "sentence_count: 60",
        "website_segmented_count: 8",
        "contact_website_segmented_count: 4",
    ]
    assert card_module._sentence_metadata_lines(_language_card_stats()) == []
