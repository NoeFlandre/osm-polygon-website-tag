"""Tests for build_card."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from textwrap import dedent

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from tests.fixtures.card import (
    _golden_card_stats,
    _golden_geometry_stats,
    _public_row,
    _setup_minimal_run,
)

import osm_polygon_website_tag.reporting.card as card_module
import osm_polygon_website_tag.reporting.card_stats as card_stats_module
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
)
from osm_polygon_website_tag.reporting.card import (
    _render_snapshot_section,
    build_card,
)
from osm_polygon_website_tag.reporting.card_stats import CardStats
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary
from osm_polygon_website_tag.reporting.geometry_stats import (
    GeometryStats,
)
from osm_polygon_website_tag.reporting.text_population import TextPopulationSummary


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


def test_rendered_front_matter_is_valid_yaml_with_declared_dataset_contract() -> None:
    front_matter = card_module._render_yaml_front_matter(_golden_card_stats())
    document = yaml.safe_load(front_matter.removeprefix("---\n").removesuffix("\n---"))

    assert document["license"] == "odbl"
    assert document["configs"][0]["data_files"] == [
        {"split": "polygons", "path": "polygons/*.parquet"}
    ]
    assert document["public_row_count"] == 3


def test_render_card_bundle_retains_the_text_population_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_population = TextPopulationSummary()
    summary = PolygonDensitySummary(3, 0, 0, ())
    geometry = GeometryStats(row_count=1)

    monkeypatch.setattr(
        card_module,
        "compute_text_population_summary",
        lambda *_args, **_kwargs: text_population,
    )
    monkeypatch.setattr(
        card_module,
        "compute_polygon_density_summary",
        lambda *_args, **_kwargs: summary,
    )
    monkeypatch.setattr(
        card_module,
        "compute_card_stats",
        lambda *_args, **_kwargs: CardStats(public_row_count=1),
    )
    monkeypatch.setattr(
        card_module,
        "compute_geometry_stats",
        lambda *_args, **_kwargs: geometry,
    )
    monkeypatch.setattr(card_module, "_public_schema_for_card", lambda *_args: pa.schema([]))
    monkeypatch.setattr(card_module, "_render_markdown", lambda *_args, **_kwargs: "body")
    monkeypatch.setattr(card_module, "_render_yaml_front_matter", lambda *_args: "front")

    bundle = card_module.render_card_bundle(tmp_path)

    assert bundle.text_population is text_population


def test_build_card_forwards_custom_yaml_source_to_bundle_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = GeometryStats(row_count=1)
    bundle = card_module.CardBundle(
        readme=b"front\nbody",
        dataset_yaml=b"front",
        summary=PolygonDensitySummary(3, 0, 0, ()),
        text_population=TextPopulationSummary(),
        geometry=geometry,
    )
    received: list[bytes | None] = []

    def render_bundle(
        _root: Path,
        *,
        source_names: Collection[str] | None,
        _yaml_source: bytes | None,
    ) -> card_module.CardBundle:
        del source_names
        received.append(_yaml_source)
        return bundle

    monkeypatch.setattr(card_module, "render_card_bundle", render_bundle)
    monkeypatch.setattr(card_module, "build_polygon_density_map", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(card_module, "_staged_geometry_stats", lambda *_args: [])
    monkeypatch.setattr(card_module, "atomic_promote_bundle", lambda _promotions: None)

    card_module.build_card(tmp_path, _yaml_source=b"custom")

    assert received == [b"custom"]


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
    byte_writes: list[tuple[Path, bytes]] = []
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

    def fake_write_bytes(path: Path, data: bytes) -> int:
        byte_writes.append((path, data))
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
    monkeypatch.setattr(Path, "write_bytes", fake_write_bytes)
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
    assert byte_writes == [
        (run_dir / ".README.md.building", b"front\nbody"),
        (run_dir / ".dataset.yaml.building", b"front"),
    ]
    assert writes == [(run_dir / ".stats.json.building", "stats", "utf-8")]
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
