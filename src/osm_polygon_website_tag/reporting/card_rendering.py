"""Pure Markdown renderers for the dataset card.

This module consumes artifact-derived summaries and has no filesystem or
release-promotion side effects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pyarrow as pa

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.reporting.card_metadata import _dataset_status_value
from osm_polygon_website_tag.reporting.card_stats import CardStats
from osm_polygon_website_tag.reporting.geographic.layout import (
    HERO_ASSET_REL_PATH,
    POLYGON_DENSITY_ASSET_REL_PATH,
)
from osm_polygon_website_tag.reporting.geometry_stats import GEOMETRY_STATS_FILENAME, GeometryStats
from osm_polygon_website_tag.runtime.config import DEFAULT_GITHUB_REPO, TRACKIO_DASHBOARD_URL

CARD_TOP_LANGUAGE_LIMIT = 10

# A public card is read, not audited. The long tails belong in the published
# Parquet and `stats.json`; the card shows enough to characterise them.
CARD_TOP_UNSUPPORTED_LIMIT = 5
CARD_TOP_HOSTNAME_LIMIT = 5


def render_markdown(
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
        *_render_geographic_section(stats),
        *_render_polygon_geometry_section(geometry),
        *_hostname_sections(stats),
        *_render_dataset_contents_section(),
        *_render_schema_section(schema),
        *_render_methodology_section(stats),
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
            "OpenStreetMap polygons that carry a `website` or `contact:website` tag, "
            "with the full main-page text of each site. Every number below is "
            "recomputed from the published Parquet files."
        ),
        "",
    ]


def _render_snapshot_section(stats: CardStats) -> list[str]:
    """Render snapshot status and artifact counts."""
    return [
        "## At a glance",
        "",
        "| | |",
        "| --- | ---: |",
        f"| Polygons | {stats.public_row_count:,} |",
        f"| With extracted text | {stats.polygons_with_any_text:,} |",
        (f"| Words of text | {stats.website_total_words + stats.contact_website_total_words:,} |"),
        f"| Languages | {stats.detected_language_count:,} |",
        f"| Regional sources | {stats.sources_count:,} / {stats.expected_sources_count:,} |",
        f"| Duplicate objects removed | {stats.duplicate_count:,} |",
        f"| Candidates rejected | {stats.rejection_count:,} |",
        f"| Status | {_dataset_status_label(stats)} |",
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
            f"Counts are unique `(osm_type, osm_id)` polygons -- {combined_words:,} words "
            "in total. Regional overlap duplicates are removed globally."
        ),
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
            "Detected with GlotLID v3. Labels are script-aware `language_Script` codes; "
            "each row carries its top-1 probability."
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
    if not stats.total_sentence_count and not stats.sentence_split_eligible_count:
        return []
    segmented, supported, unsupported, eligible = _sentence_counts(stats)
    coverage = _percentage(supported, eligible)
    unsupported_share = _percentage(unsupported, eligible)
    top_rows = _unsupported_language_rows(stats)
    mean_sentences = _mean_sentences(stats.total_sentence_count, segmented)
    return [
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
        f"| Sentences | {stats.total_sentence_count:,} |",
        f"| Segmented `website` texts | {stats.website_sentence_row_count:,} |",
        f"| Segmented `contact:website` texts | {stats.contact_website_sentence_row_count:,} |",
        f"| Eligible text units for splitting | {eligible:,} |",
        f"| Split with a supported language | {supported:,} |",
        f"| Left unsplit: unsupported language | {unsupported:,} |",
        f"| Sentence-splitting coverage | {coverage} |",
        f"| Unsupported-language share | {unsupported_share} |",
        "",
        f"Mean sentences per segmented text: **{mean_sentences}**",
        "",
        f"Most common unsupported languages: {top_rows}.",
        "",
    ]


def _sentence_counts(stats: CardStats) -> tuple[int, int, int, int]:
    """Return segmented, supported, unsupported, and eligible text-unit counts."""
    segmented = stats.website_sentence_row_count + stats.contact_website_sentence_row_count
    supported = _reported_count(stats.sentence_split_supported_count, segmented)
    unsupported = _reported_count(
        stats.sentence_split_unsupported_count,
        stats.unsupported_language_row_count,
    )
    eligible = _reported_count(stats.sentence_split_eligible_count, supported + unsupported)
    return segmented, supported, unsupported, eligible


def _reported_count(value: int, fallback: int) -> int:
    """Use a newer explicit metric while retaining compatibility with old cards."""
    return value or fallback


def _unsupported_language_rows(stats: CardStats) -> str:
    """Name the largest unsupported languages inline rather than in a long table.

    The full distribution is hundreds of labels with a very long tail; it lives
    in the published Parquet, and listing it on the card buried everything else.
    """
    top = stats.top_unsupported_sentence_languages[:CARD_TOP_UNSUPPORTED_LIMIT]
    if not top:
        return "none reported"
    return ", ".join(f"`{label}` ({count:,})" for label, count in top)


def _mean_sentences(total: int, segmented: int) -> str:
    """Format the sentence mean without dividing by an unsupported-only population."""
    if not segmented:
        return "n/a"
    return f"{total / segmented:.1f}"


def _percentage(numerator: int, denominator: int) -> str:
    """Format a percentage without inventing a value for an empty population."""
    if not denominator:
        return "n/a"
    return f"{100 * numerator / denominator:.1f}%"


def _render_polygon_geometry_section(geometry: GeometryStats) -> list[str]:
    """Render the headline polygon surface and shape statistics."""
    area = geometry.area.summary
    return [
        "## Polygon geometry",
        "",
        (
            "Geodesic areas on the WGS84 ellipsoid, over every published polygon row. "
            f"Full breakdown in [`{GEOMETRY_STATS_FILENAME}`]({GEOMETRY_STATS_FILENAME})."
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
            f"**{stats.occupied_h3_cell_count:,}** occupied H3 cells at resolution "
            f"{stats.polygon_density_h3_resolution}, covering "
            f"**{stats.polygon_density_row_count:,}** unique polygons with extracted text. "
            "Log colour scale, Natural Earth 1:110m backdrop."
        ),
        "",
    ]


def _render_methodology_section(stats: CardStats) -> list[str]:
    """Render extraction, status, and URL-safety methodology."""
    return [
        "## Method",
        "",
        (
            "- Geometry assembled with libosmium; text extracted with Trafilatura "
            "and never truncated. Word counts are Unicode `\\w+` matches."
        ),
        (
            "- Text status is one of `absent`, `pending`, `success`, `empty`, "
            "`invalid_url`, `unsafe_url`, `fetch_error`, `extract_error`. "
            + _enrichment_policy(stats)
        ),
        (
            "- URLs resolving to anything other than a public IP are refused as "
            "`unsafe_url`, before and after redirects."
        ),
        (
            f"- [Live metrics]({TRACKIO_DASHBOARD_URL}) \u00b7 "
            f"[source code]({DEFAULT_GITHUB_REPO.removesuffix('.git')})"
        ),
        "",
    ]


def _render_dataset_contents_section() -> list[str]:
    """Render the public artifact inventory."""
    return [
        "## Dataset contents",
        "",
        "- `polygons/*.parquet` -- the polygons and their extracted text, one shard per source.",
        "- `analysis/*.parquet` -- languages, sentences, hostnames, duplicates and per-source counts.",
        f"- `{GEOMETRY_STATS_FILENAME}`, `deduplication_summary.json` -- full geometry and dedup numbers.",
        "- `manifests/` -- source inventory and completion receipt.",
        "",
    ]


def _render_schema_section(schema: pa.Schema) -> list[str]:
    """Render the selected public polygon schema."""
    return [
        "## Public polygon schema",
        "",
        "| Column | Type | Nullable |",
        "| --- | --- | :---: |",
        *_schema_rows(schema),
        "",
    ]


def _render_provenance_section() -> list[str]:
    """Render provenance and licensing terms."""
    return [
        "## Provenance and license",
        "",
        (
            "Every artifact is bound by relative path, byte size and SHA-256 in the "
            "completion receipt. Map backdrop: Natural Earth 1:110m (public domain)."
        ),
        "",
        (
            "© OpenStreetMap contributors, under the "
            "[ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) -- see the "
            "[copyright page](https://www.openstreetmap.org/copyright). "
            "Extracts from [Geofabrik](https://download.geofabrik.de/)."
        ),
        "",
        (
            "**The website text is not covered by the ODbL.** It is third-party "
            "content; rights stay with each source site. Check a site's terms "
            "before reusing its text."
        ),
        "",
    ]


def _render_citation_section() -> list[str]:
    """Render the machine-readable citation reference."""
    return [
        "## Citation",
        "",
        (
            "Machine-readable metadata: [`CITATION.cff`]"
            "(https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag/"
            "blob/main/CITATION.cff)."
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
    if sections:
        # The joined section ends on a table row; the next heading needs air.
        sections.append("")
    return sections


def _schema_rows(schema: pa.Schema = POLYGON_PUBLIC_SCHEMA) -> list[str]:
    """Render one Markdown row for every public polygon schema field."""
    return [
        f"| `{field.name}` | `{field.type}` | {'yes' if field.nullable else 'no'} |"
        for field in schema
    ]


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
    """Render the largest artifact-derived hostnames."""
    lines = [f"### Top `{label}` hostnames", ""]
    if not rows:
        lines.append("_No hostnames observed._")
        return "\n".join(lines)
    lines.extend(["| Hostname | Polygons |", "| --- | ---: |"])
    for row in rows[:CARD_TOP_HOSTNAME_LIMIT]:
        hostname = row[hostname_key]
        row_count = row["row_count"]
        if not isinstance(hostname, str) or not isinstance(row_count, int):
            raise ValueError("invalid hostname analysis row")
        lines.append(f"| `{hostname}` | {row_count:,} |")
    return "\n".join(lines)
