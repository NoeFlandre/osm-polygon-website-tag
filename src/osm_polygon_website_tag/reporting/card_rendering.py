"""Pure Markdown renderers for the dataset card.

This module consumes artifact-derived summaries and has no filesystem or
release-promotion side effects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pyarrow as pa

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA, column_doc
from osm_polygon_website_tag.reporting.card_stats import CardStats
from osm_polygon_website_tag.reporting.geographic.layout import (
    HERO_ASSET_REL_PATH,
    POLYGON_DENSITY_ASSET_REL_PATH,
)
from osm_polygon_website_tag.reporting.geometry_stats import GEOMETRY_STATS_FILENAME, GeometryStats
from osm_polygon_website_tag.runtime.config import DEFAULT_GITHUB_REPO, TRACKIO_DASHBOARD_URL

CARD_TOP_LANGUAGE_LIMIT = 10


def _render_markdown(
    stats: CardStats,
    *,
    geometry: GeometryStats,
    schema: pa.Schema = POLYGON_PUBLIC_SCHEMA,
) -> str:
    """Render a concise public-facing card from artifact-derived statistics."""
    parts = [
        *_render_intro_section(),
        *_render_snapshot_section(stats),
        *_render_website_text_section(stats),
        *_render_language_section(stats),
        *_render_sentence_section(stats),
        *_render_polygon_geometry_section(geometry),
        *_render_geographic_section(stats),
        *_render_links_section(),
        *_hostname_sections(stats),
        *_render_methodology_section(stats),
        *_render_dataset_contents_section(),
        *_render_schema_section(schema),
        *_render_provenance_section(),
        *_render_citation_section(),
    ]
    return "\n".join(parts) + "\n"


def _render_intro_section() -> list[str]:
    """Render the card title, banner, and dataset description."""
    return [
        "# OSM Polygon Website Dataset",
        "",
        f"![osm-polygon-website-tag hero banner]({HERO_ASSET_REL_PATH})",
        "",
        (
            "OpenStreetMap closed ways and polygon relations carrying a non-empty "
            "`website` OR `contact:website` tag, with full main-page text extracted "
            "using Trafilatura. Every statistic below is regenerated from the "
            "current upload-acknowledged Parquet artifacts."
        ),
        "",
    ]


def _render_snapshot_section(stats: CardStats) -> list[str]:
    """Render snapshot status and artifact counts."""
    return [
        "## Snapshot",
        "",
        "| Metric | Value | What it means |",
        "| --- | ---: | --- |",
        f"| Snapshot status | {_dataset_status_label(stats)} | Current published snapshot |",
        (
            f"| Regional PBFs included | {stats.sources_count:,} / "
            f"{stats.expected_sources_count:,} | Published source shards / expected source PBFs |"
        ),
        (
            f"| Published polygon rows | {stats.public_row_count:,} | "
            "Rows in the public `polygons/` files |"
        ),
        (
            f"| Comparison observations | {stats.observation_count:,} | "
            "Source-level records with a website, contact:website, or Wikidata tag |"
        ),
        (
            f"| Duplicate OSM objects | {stats.duplicate_count:,} | "
            "Objects observed in more than one source snapshot |"
        ),
        (
            f"| Conflicting snapshot observations | {stats.conflicting_snapshot_count:,} | "
            "Repeated observations whose tag values disagree with the selected version |"
        ),
        (
            f"| Rejected polygon candidates | {stats.rejection_count:,} | "
            "Candidate objects that did not produce a usable polygon row |"
        ),
        "",
    ]


def _render_website_text_section(stats: CardStats) -> list[str]:
    """Render extracted-text counts for both supported website tags."""
    combined_words = stats.website_total_words + stats.contact_website_total_words
    return [
        "## Website text",
        "",
        "| Tag | URLs | Successful | Empty | Failed | Words |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| `website` | {stats.website_urls_present:,} | "
            f"{stats.website_text_success_count:,} | {stats.website_text_empty_count:,} | "
            f"{stats.website_text_failure_count:,} | {stats.website_total_words:,} |"
        ),
        (
            f"| `contact:website` | {stats.contact_website_urls_present:,} | "
            f"{stats.contact_website_text_success_count:,} | "
            f"{stats.contact_website_text_empty_count:,} | "
            f"{stats.contact_website_text_failure_count:,} | "
            f"{stats.contact_website_total_words:,} |"
        ),
        "",
        (
            "Website-text table counts are unique `(osm_type, osm_id)` identities across "
            "regional rows; regional overlap duplicates are removed globally."
        ),
        "",
        f"Unique polygons with extracted text: **{stats.polygons_with_any_text:,}**  ",
        (
            "Counts unique `(osm_type, osm_id)` polygons across regional rows when any copy "
            "has successful, trimmed non-empty website or contact:website text; regional "
            "overlap duplicates removed globally."
        ),
        f"Combined extracted words: **{combined_words:,}**",
        "",
    ]


def _render_language_section(stats: CardStats) -> list[str]:
    """Render detected-language totals when the run carries v1.4 labels."""
    if not stats.detected_language_count:
        return []
    top = stats.top_languages[:CARD_TOP_LANGUAGE_LIMIT]
    return [
        "## Languages",
        "",
        (
            "Detected with GlotLID v3 on successfully extracted text; labels are exact "
            "script-aware `language_Script` codes with a top-1 probability column."
        ),
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Distinct languages | {stats.detected_language_count:,} |",
        f"| Labeled `website` texts | {stats.website_language_count:,} |",
        f"| Labeled `contact:website` texts | {stats.contact_website_language_count:,} |",
        "",
        f"Top {len(top)} labels across both tags:",
        "",
        "| Language | Texts |",
        "| --- | ---: |",
        *(f"| `{label}` | {count:,} |" for label, count in top),
        "",
    ]


def _render_sentence_section(stats: CardStats) -> list[str]:
    """Render sentence totals when the run carries v1.5 segmentation."""
    if not stats.total_sentence_count:
        return []
    segmented = stats.website_sentence_row_count + stats.contact_website_sentence_row_count
    return [
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
        f"| Sentences | {stats.total_sentence_count:,} |",
        f"| Segmented `website` texts | {stats.website_sentence_row_count:,} |",
        f"| Segmented `contact:website` texts | {stats.contact_website_sentence_row_count:,} |",
        f"| Texts in an uncovered language | {stats.unsupported_language_row_count:,} |",
        "",
        f"Mean sentences per segmented text: **{stats.total_sentence_count / segmented:.1f}**",
        "",
    ]


def _render_polygon_geometry_section(geometry: GeometryStats) -> list[str]:
    """Render the headline polygon surface and shape statistics."""
    area = geometry.area.summary
    return [
        "## Polygon geometry",
        "",
        (
            "Surface and shape statistics computed over every published polygon row from the "
            "`area_m2`, `bbox`, and `geometry` columns. Areas are geodesic on the WGS84 "
            f"ellipsoid. Population scope: published polygon rows. The complete breakdown is published as [`{GEOMETRY_STATS_FILENAME}`]"
            f"({GEOMETRY_STATS_FILENAME})."
        ),
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Polygons measured | {geometry.row_count:,} |",
        f"| Total area | {area.total / 1_000_000:,.2f} km² |",
        f"| Median area | {area.median:,.2f} m² |",
        f"| Mean area | {area.mean:,.2f} m² |",
        f"| Smallest / largest area | {area.minimum:,.2f} / {area.maximum:,.2f} m² |",
        f"| p95 area | {area.percentiles.get('p95', 0.0):,.2f} m² |",
        f"| MultiPolygon rows | {geometry.shape.multipolygon_row_count:,} |",
        f"| Rows with holes | {geometry.shape.with_holes_row_count:,} |",
        f"| Rows below 1 m² | {geometry.area.below_one_m2_row_count:,} |",
        "",
        f"Dataset bounding box: `{_render_bbox(geometry.extent.bbox)}` "
        "(min lon, min lat, max lon, max lat).",
        "",
    ]


def _render_bbox(bbox: list[float] | None) -> str:
    """Render the dataset bounding box, or its absence, deterministically."""
    if bbox is None:
        return "none"
    return "[" + ", ".join(f"{value:.6f}" for value in bbox) + "]"


def _render_geographic_section(stats: CardStats) -> list[str]:
    """Render the extracted-text polygon density summary."""
    return [
        "## Geographic distribution",
        "",
        (
            f"![H3 polygon density]({POLYGON_DENSITY_ASSET_REL_PATH})\n\n"
            f"H3 resolution {stats.polygon_density_h3_resolution} contains "
            f"**{stats.occupied_h3_cell_count:,}** occupied cells across "
            f"**{stats.polygon_density_row_count:,}** unique polygons with successfully "
            "extracted, non-empty website or contact:website text, globally deduplicated by "
            "`(osm_type, osm_id)`; regional overlap duplicates removed globally. "
            "The color scale is logarithmic, counts are absolute, and a Natural Earth "
            "1:110m land backdrop provides geographic context."
        ),
        "",
    ]


def _render_links_section() -> list[str]:
    """Render links to the live metrics and source repository."""
    return [
        "## Links",
        "",
        (
            f"Live metrics: [Trackio dashboard]({TRACKIO_DASHBOARD_URL}); "
            "it shows this frozen dataset snapshot."
        ),
        (
            "Code and README: "
            f"[GitHub repository and README]({DEFAULT_GITHUB_REPO.removesuffix('.git')})."
        ),
        "",
    ]


def _render_methodology_section(stats: CardStats) -> list[str]:
    """Render extraction, status, and URL-safety methodology."""
    return [
        "## Methodology and quality",
        "",
        (
            "Geometry is assembled with libosmium. Full main text is extracted "
            "independently for both website tags with Trafilatura and is not "
            "truncated. Word counts are Python Unicode `\\w+` matches."
        ),
        "",
        (
            "Text statuses are `absent`, `pending`, `success`, `empty`, "
            "`invalid_url`, `unsafe_url`, `fetch_error`, or `extract_error`. "
            + _enrichment_policy(stats)
        ),
        "",
        (
            "A URL is marked `unsafe_url` when its hostname, or any redirect "
            "target, does not resolve exclusively to globally routable public "
            "IP addresses. Localhost, private, reserved, multicast, and "
            "unspecified targets are blocked. Unsupported schemes and URLs "
            "containing credentials are classified as `invalid_url`; redirect "
            "limits, timeouts, oversized responses, and unsupported content "
            "types are recorded as `fetch_error`."
        ),
        "",
    ]


def _render_dataset_contents_section() -> list[str]:
    """Render the public artifact inventory."""
    return [
        "## Dataset contents",
        "",
        "- `polygons/*.parquet`: the public polygon split, one shard per source PBF.",
        "- `analysis/*.parquet`: detailed overlap, provenance, hostname, duplicate, "
        "conflict, and per-source statistics.",
        "- `deduplication_summary.json`: counts and tag-conflict totals from the global "
        "canonicalization pass.",
        f"- `{GEOMETRY_STATS_FILENAME}`: complete machine-readable polygon geometry statistics.",
        "- `manifests/`: source inventory, upload checkpoints, and completion receipt.",
        "",
    ]


def _render_schema_section(schema: pa.Schema) -> list[str]:
    """Render the selected public polygon schema."""
    return [
        "## Public polygon schema",
        "",
        "| Column | Type | Nullable | Description |",
        "| --- | --- | :---: | --- |",
        *_schema_rows(schema),
        "",
    ]


def _render_provenance_section() -> list[str]:
    """Render provenance and licensing terms."""
    return [
        "## Provenance and license",
        "",
        (
            "Source filename, byte size, and nanosecond modification time are "
            "recorded before processing. The completion receipt binds finalized "
            "artifacts by relative path, byte size, and SHA-256."
        ),
        "",
        (
            "The map backdrop uses Natural Earth 1:110m Admin-0 country geography, "
            "distributed in the source tree under its public-domain terms."
        ),
        "",
        (
            "© OpenStreetMap contributors. OpenStreetMap data is available under "
            "the [Open Database License (ODbL) 1.0]"
            "(https://opendatacommons.org/licenses/odbl/1-0/); see the "
            "[OpenStreetMap copyright and attribution page]"
            "(https://www.openstreetmap.org/copyright). Regional PBF extracts are "
            "provided by [Geofabrik](https://download.geofabrik.de/)."
        ),
        "",
        (
            "Website text is third-party content, separate from the OSM data, and "
            "is not covered by the ODbL. This dataset asserts no license for that "
            "text and grants no additional reuse rights: copyright and licensing "
            "conditions remain with each source website. Check the source site's "
            "terms or license before using or redistributing extracted text."
        ),
        "",
    ]


def _render_citation_section() -> list[str]:
    """Render the machine-readable citation reference."""
    return [
        "## Citation",
        "",
        (
            "If you use this dataset, please cite it using the machine-readable "
            "metadata in [`CITATION.cff`]"
            "(https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag/"
            "blob/main/CITATION.cff). GitHub and the Hugging Face dataset page "
            "can then display the citation directly."
        ),
        "",
        (
            "> Flandre, Noé. *OSM Polygon Website Tag Dataset*. "
            "[Hugging Face dataset]"
            "(https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag)"
        ),
    ]


def _enrichment_policy(stats: CardStats) -> str:
    """Describe whether later resumptions may retry failed text fetches."""
    if stats.snapshot_status == "done":
        return (
            "A source is enriched only when every status is `success` or `absent`. "
            "This snapshot is frozen: failed values remain as recorded and are not "
            "retried. Successful values are cached."
        )
    return (
        "A source is enriched only when every status is `success` or `absent`. "
        "Failed values retry on later resumptions; successful values are cached."
    )


def _hostname_sections(stats: CardStats) -> list[str]:
    """Render optional hostname sections without inventing empty sections."""
    sections: list[str] = []
    for label, rows, key in (
        ("website", stats.top_hostnames_website, "website_hostname"),
        ("contact:website", stats.top_hostnames_contact_website, "contact_website_hostname"),
    ):
        if rows:
            sections.extend(["", _render_hostnames(label, rows, hostname_key=key)])
    return sections


def _schema_rows(schema: pa.Schema = POLYGON_PUBLIC_SCHEMA) -> list[str]:
    """Render one Markdown row for every public polygon schema field."""
    rows: list[str] = []
    for field in schema:
        description = " ".join(column_doc(field.name).split()).replace("|", "\\|")
        rows.append(
            f"| `{field.name}` | `{field.type}` | "
            f"{'yes' if field.nullable else 'no'} | {description} |"
        )
    return rows


def _dataset_status_value(stats: CardStats) -> str:
    """Return the stable machine-readable status shown in card metadata."""
    if stats.snapshot_status == "done":
        return "done"
    if (
        stats.expected_sources_count > 0
        and stats.enriched_sources_count == stats.expected_sources_count
    ):
        return "complete"
    return "in_progress"


def _dataset_status_label(stats: CardStats) -> str:
    """Return a short human-readable status label for the snapshot table."""
    return {
        "done": "Done",
        "complete": "Complete",
        "in_progress": "In progress",
    }[_dataset_status_value(stats)]


def _render_hostnames(
    label: str,
    rows: Sequence[Mapping[str, object]],
    *,
    hostname_key: str,
) -> str:
    """Render at most ten artifact-derived hostnames."""
    lines = [f"### Top `{label}` hostnames", ""]
    if not rows:
        lines.append("_No hostnames observed._")
        return "\n".join(lines)
    lines.extend(["| Hostname | Polygons |", "| --- | ---: |"])
    for row in rows[:10]:
        hostname = row[hostname_key]
        row_count = row["row_count"]
        if not isinstance(hostname, str) or not isinstance(row_count, int):
            raise ValueError("invalid hostname analysis row")
        lines.append(f"| `{hostname}` | {row_count:,} |")
    return "\n".join(lines)
