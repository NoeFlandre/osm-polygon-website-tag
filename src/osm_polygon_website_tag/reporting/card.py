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

from pathlib import Path

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA, column_doc
from osm_polygon_website_tag.reporting.card_stats import CardStats, compute_card_stats
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle


def build_card(run_dir: Path | str) -> Path:
    """Build (or rebuild) the README card for ``run_dir``.

    Returns the path to ``README.md``. The function is idempotent:
    running it twice with the same inputs produces the same output
    bytes.
    """
    run_dir = Path(run_dir)
    stats = compute_card_stats(run_dir)
    body = _render_markdown(stats)
    front_matter = _render_yaml_front_matter(stats)
    readme = front_matter + "\n" + body
    path = run_dir / "README.md"
    yaml_path = run_dir / "dataset.yaml"
    staged_readme = run_dir / ".README.md.building"
    staged_yaml = run_dir / ".dataset.yaml.building"
    staged_readme.write_text(readme)
    staged_yaml.write_text(front_matter)
    atomic_promote_bundle([(staged_readme, path), (staged_yaml, yaml_path)])
    return path


def _render_yaml_front_matter(stats: CardStats) -> str:
    """Render the HF YAML front matter block.

    License identifier is Open Database License (ODbL) v1.0 -- the
    canonical license for OpenStreetMap data.
    """
    lines = [
        "---",
        "license: odbl",
        "task_categories:",
        "  - geographic-information-retrieval",
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
    """Render the body of the README card."""
    parts: list[str] = []
    parts.append("# OSM Polygon Website Tag Dataset")
    parts.append("")
    parts.append(
        "Per-polygon website and Wikidata tags extracted from "
        "OpenStreetMap PBF extracts. This card is auto-generated from "
        "the run artifacts; every number below is reproducible from "
        "the Parquet files in this repository."
    )
    parts.append("")

    parts.append("## Inclusion rule")
    parts.append("")
    parts.append(
        "A public row is an assembled closed OSM way or supported polygon "
        "relation with a non-empty `website` OR `contact:website` tag. "
        "`wikidata` is retained for comparison but is not an inclusion requirement."
    )
    parts.append("")

    parts.append("## Polygon columns")
    parts.append("")
    parts.append("| Column | Arrow type | Nullable | Description |")
    parts.append("| --- | --- | --- | --- |")
    for field in POLYGON_PUBLIC_SCHEMA:
        description = " ".join(column_doc(field.name).split()).replace("|", "\\|")
        parts.append(
            f"| `{field.name}` | `{field.type}` | "
            f"{'yes' if field.nullable else 'no'} | {description} |"
        )
    parts.append("")

    parts.append("## Summary")
    parts.append("")
    parts.append(f"- Source PBFs processed: {stats.sources_count}")
    parts.append(
        f"- Enriched source PBFs: {stats.enriched_sources_count} / {stats.expected_sources_count}"
    )
    parts.append(f"- Public polygon rows: {stats.public_row_count}")
    parts.append(f"- Comparison observations (pre-dedup): {stats.observation_count}")
    parts.append(f"- Canonical polygons (post-dedup): {stats.canonical_count}")
    parts.append(f"- Duplicate observations: {stats.duplicate_count}")
    parts.append(f"- Conflicting snapshots: {stats.conflicting_snapshot_count}")
    parts.append(f"- Rejections: {stats.rejection_count}")
    parts.append("")

    parts.append("## Website text enrichment")
    parts.append("")
    parts.append(
        "Full main text is extracted independently from both `website` and "
        "`contact:website` using Trafilatura. Text is not truncated. Word counts "
        "are the number of Python Unicode `\\w+` matches in the stored text."
    )
    parts.append("")
    parts.append(f"- Website URLs present: {stats.website_urls_present}")
    parts.append(f"- Website successful extractions: {stats.website_text_success_count}")
    parts.append(f"- Website empty extractions: {stats.website_text_empty_count}")
    parts.append(f"- Website failed extractions: {stats.website_text_failure_count}")
    parts.append(f"- Website extracted words: {stats.website_total_words}")
    parts.append(f"- Contact website URLs present: {stats.contact_website_urls_present}")
    parts.append(
        f"- Contact website successful extractions: {stats.contact_website_text_success_count}"
    )
    parts.append(f"- Contact website empty extractions: {stats.contact_website_text_empty_count}")
    parts.append(
        f"- Contact website failed extractions: {stats.contact_website_text_failure_count}"
    )
    parts.append(f"- Contact website extracted words: {stats.contact_website_total_words}")
    parts.append(f"- Polygons with at least one extracted text: {stats.polygons_with_any_text}")
    parts.append("")
    parts.append(
        "Statuses are `absent`, `pending`, `success`, `empty`, `invalid_url`, "
        "`unsafe_url`, `fetch_error`, or `extract_error`. Failed values are retried "
        "on a later pipeline invocation."
    )
    parts.append("")

    parts.append("## Eight-cell provenance cube")
    parts.append("")
    parts.append("| Cell | observation | canonical |")
    parts.append("| --- | ---: | ---: |")
    for cell, _ in sorted(stats.eight_cell_observation.items()):
        obs = stats.eight_cell_observation.get(cell, 0)
        can = stats.eight_cell_canonical.get(cell, 0)
        parts.append(f"| `{cell}` | {obs} | {can} |")
    parts.append("")

    parts.append("## Top hostnames (website)")
    parts.append("")
    if stats.top_hostnames_website:
        parts.append("| Hostname | Count |")
        parts.append("| --- | ---: |")
        for r in stats.top_hostnames_website[:25]:
            parts.append(f"| `{r['website_hostname']}` | {r['row_count']} |")
    else:
        parts.append("_No website hostnames observed._")
    parts.append("")

    parts.append("## Top hostnames (contact:website)")
    parts.append("")
    if stats.top_hostnames_contact_website:
        parts.append("| Hostname | Count |")
        parts.append("| --- | ---: |")
        for r in stats.top_hostnames_contact_website[:25]:
            parts.append(f"| `{r['contact_website_hostname']}` | {r['row_count']} |")
    else:
        parts.append("_No contact:website hostnames observed._")
    parts.append("")

    parts.append("## Per-source coverage")
    parts.append("")
    if stats.per_source_counts:
        parts.append("| Source PBF | Public rows |")
        parts.append("| --- | ---: |")
        for r in stats.per_source_counts:
            parts.append(f"| `{r['source_pbf']}` | {r['row_count']} |")
    else:
        parts.append("_No public rows._")
    parts.append("")

    parts.append("## Provenance")
    parts.append("")
    parts.append(
        "Each public polygon row is derived deterministically from a source PBF "
        "whose filename, byte size, and modification time were recorded before "
        "processing. `manifests/completion_receipt.json` binds every "
        "published artifact by relative path, byte size, and SHA-256."
    )
    parts.append("")
    parts.append(
        "© OpenStreetMap contributors. OpenStreetMap data is available under "
        "the [Open Database License (ODbL) 1.0]"
        "(https://opendatacommons.org/licenses/odbl/1-0/); see the "
        "[OpenStreetMap copyright and attribution page]"
        "(https://www.openstreetmap.org/copyright). Regional PBF extracts are "
        "provided by [Geofabrik](https://download.geofabrik.de/)."
    )
    return "\n".join(parts) + "\n"
