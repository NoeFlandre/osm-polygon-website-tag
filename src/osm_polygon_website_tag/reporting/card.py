"""Build the README card for a finalized run.

The card is a static Markdown document with HF YAML front matter.
Every number it contains is recomputed from the published artifacts
by :mod:`osm_polygon_website_tag.reporting.card_stats`; the card builder does
not recompute, classify, or transform anything itself.

The card is generated (or regenerated) by :func:`build_card`. It
writes:

* ``<run_dir>/README.md`` -- the rendered card (with YAML front matter)
* ``<run_dir>/dataset.yaml`` -- machine-readable dataset-card metadata

The card is read-only by construction -- it contains no pointers to
mutable run state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA, column_doc
from osm_polygon_website_tag.reporting.card_stats import CardStats, compute_card_stats
from osm_polygon_website_tag.reporting.geographic.aggregation import compute_polygon_density_summary
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geographic.polygon_density import build_polygon_density_map
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle


def build_card(run_dir: Path | str) -> Path:
    """Build (or rebuild) the README card for ``run_dir``.

    Returns the path to ``README.md``. The function is idempotent:
    running it twice with the same inputs produces the same output
    bytes.
    """
    run_dir = Path(run_dir)
    summary = compute_polygon_density_summary(run_dir)
    stats = compute_card_stats(run_dir, summary=summary)
    body = _render_markdown(stats)
    front_matter = _render_yaml_front_matter(stats)
    readme = front_matter + "\n" + body
    path = run_dir / "README.md"
    yaml_path = run_dir / "dataset.yaml"
    staged_readme = run_dir / ".README.md.building"
    staged_yaml = run_dir / ".dataset.yaml.building"
    staged_map = run_dir / ".assets" / "geographic_polygon_density.png.building"
    staged_map.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_polygon_density_map(run_dir, summary=summary, output_path=staged_map)
        staged_readme.write_text(readme, encoding="utf-8")
        staged_yaml.write_text(front_matter, encoding="utf-8")
        atomic_promote_bundle(
            [
                (staged_map, run_dir / POLYGON_DENSITY_ASSET_REL_PATH),
                (staged_readme, path),
                (staged_yaml, yaml_path),
            ]
        )
    finally:
        staged_map.unlink(missing_ok=True)
        staged_readme.unlink(missing_ok=True)
        staged_yaml.unlink(missing_ok=True)
    return path


def _render_yaml_front_matter(stats: CardStats) -> str:
    """Render the HF YAML front matter block.

    License identifier is Open Database License (ODbL) v1.0 -- the
    canonical license for OpenStreetMap data.
    """
    lines = [
        "---",
        "license: odbl",
        "tags:",
        "  - openstreetmap",
        "  - osm",
        "  - polygon",
        "  - website",
        "  - wikidata",
        "  - geographic-data",
        "size_categories:",
        f"  - {_size_category(stats.public_row_count)}",
        "configs:",
        "  - config_name: default",
        "    data_files:",
        "      - split: polygons",
        "        path: polygons/*.parquet",
        f"observation_count: {stats.observation_count}",
        f"canonical_count: {stats.canonical_count}",
        f"public_row_count: {stats.public_row_count}",
        f"rejection_count: {stats.rejection_count}",
        f"duplicate_count: {stats.duplicate_count}",
        f"conflicting_snapshot_count: {stats.conflicting_snapshot_count}",
        f"sources_count: {stats.sources_count}",
        f"expected_sources_count: {stats.expected_sources_count}",
        f"enriched_sources_count: {stats.enriched_sources_count}",
        (
            "dataset_status: complete"
            if stats.expected_sources_count > 0
            and stats.enriched_sources_count == stats.expected_sources_count
            else "dataset_status: in_progress"
        ),
        f"website_text_success_count: {stats.website_text_success_count}",
        f"website_total_words: {stats.website_total_words}",
        f"contact_website_text_success_count: {stats.contact_website_text_success_count}",
        f"contact_website_total_words: {stats.contact_website_total_words}",
        f"polygon_density_h3_resolution: {stats.polygon_density_h3_resolution}",
        f"polygon_density_row_count: {stats.polygon_density_row_count}",
        f"occupied_h3_cell_count: {stats.occupied_h3_cell_count}",
        "---",
    ]
    return "\n".join(lines)


def _size_category(row_count: int) -> str:
    """Return the Hugging Face size category derived from public rows."""
    thresholds = (
        (1_000, "n<1K"),
        (10_000, "1K<n<10K"),
        (100_000, "10K<n<100K"),
        (1_000_000, "100K<n<1M"),
        (10_000_000, "1M<n<10M"),
        (100_000_000, "10M<n<100M"),
        (1_000_000_000, "100M<n<1B"),
    )
    for upper_bound, category in thresholds:
        if row_count < upper_bound:
            return category
    return "n>1B"


def _render_markdown(stats: CardStats) -> str:
    """Render a concise public-facing card from artifact-derived statistics."""
    complete = (
        stats.expected_sources_count > 0
        and stats.enriched_sources_count == stats.expected_sources_count
    )
    combined_words = stats.website_total_words + stats.contact_website_total_words
    parts = [
        "# OSM Polygon Website Dataset",
        "",
        (
            "OpenStreetMap closed ways and polygon relations carrying a non-empty "
            "`website` OR `contact:website` tag, with full main-page text extracted "
            "using Trafilatura. Every statistic below is regenerated from the "
            "published Parquet artifacts."
        ),
        "",
        "## Snapshot",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Dataset status | {'Complete' if complete else 'In progress'} |",
        f"| Source PBFs | {stats.sources_count:,} / {stats.expected_sources_count:,} |",
        f"| Public polygons | {stats.public_row_count:,} |",
        f"| Canonical polygons | {stats.canonical_count:,} |",
        f"| Comparison observations | {stats.observation_count:,} |",
        f"| Duplicate observations | {stats.duplicate_count:,} |",
        f"| Conflicting snapshots | {stats.conflicting_snapshot_count:,} |",
        f"| Geometry rejections | {stats.rejection_count:,} |",
        "",
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
        f"Polygons with extracted text: **{stats.polygons_with_any_text:,}**  ",
        f"Combined extracted words: **{combined_words:,}**",
        "",
        "## Geographic distribution",
        "",
        (
            f"![H3 polygon density]({POLYGON_DENSITY_ASSET_REL_PATH})\n\n"
            f"H3 resolution {stats.polygon_density_h3_resolution} contains "
            f"**{stats.occupied_h3_cell_count:,}** occupied cells across "
            f"**{stats.polygon_density_row_count:,}** polygon centroids. "
            "The color scale is logarithmic, counts are absolute, and no basemap is rendered."
        ),
        "",
        _render_hostnames(
            "website",
            stats.top_hostnames_website,
            hostname_key="website_hostname",
        ),
        "",
        _render_hostnames(
            "contact:website",
            stats.top_hostnames_contact_website,
            hostname_key="contact_website_hostname",
        ),
        "",
        "## Dataset contents",
        "",
        "- `polygons/*.parquet`: the public polygon split, one shard per source PBF.",
        "- `analysis/*.parquet`: detailed overlap, provenance, hostname, duplicate, "
        "conflict, and per-source statistics.",
        "- `manifests/`: source inventory, upload checkpoints, and completion receipt.",
        "",
        "## Public polygon schema",
        "",
        "| Column | Type | Nullable | Description |",
        "| --- | --- | :---: | --- |",
    ]
    for field in POLYGON_PUBLIC_SCHEMA:
        description = " ".join(column_doc(field.name).split()).replace("|", "\\|")
        parts.append(
            f"| `{field.name}` | `{field.type}` | "
            f"{'yes' if field.nullable else 'no'} | {description} |"
        )
    parts.extend(
        [
            "",
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
                "Failed values retry on later resumptions; successful values are cached."
            ),
            "",
            "## Provenance and license",
            "",
            (
                "Source filename, byte size, and nanosecond modification time are "
                "recorded before processing. The completion receipt binds finalized "
                "artifacts by relative path, byte size, and SHA-256."
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
        ]
    )
    return "\n".join(parts) + "\n"


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
