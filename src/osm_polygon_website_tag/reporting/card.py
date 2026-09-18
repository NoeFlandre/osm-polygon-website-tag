"""Build the README card for a finalized run.

The card is a static Markdown document with HF YAML front matter.
Every number it contains is recomputed from the published artifacts
by :mod:`osm_polygon_website_tag.reporting.card_stats`; the card builder does
not recompute, classify, or transform anything itself.

The card is generated (or regenerated) by :func:`build_card`. It
writes:

* ``<run_dir>/README.md`` -- the rendered card (with YAML front matter)
* ``<run_dir>/dataset.yaml`` -- machine-readable dataset-card metadata
* ``<run_dir>/stats.json`` -- the complete polygon geometry statistics plus the
  canonical global text population used by the card and map; it is promoted
  only when its bytes change

The card is read-only by construction -- it contains no pointers to
mutable run state.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
    column_doc,
)
from osm_polygon_website_tag.reporting.card_stats import CardStats, compute_card_stats
from osm_polygon_website_tag.reporting.geographic.aggregation import compute_polygon_density_summary
from osm_polygon_website_tag.reporting.geographic.layout import (
    HERO_ASSET_REL_PATH,
    POLYGON_DENSITY_ASSET_REL_PATH,
)
from osm_polygon_website_tag.reporting.geographic.models import PolygonDensitySummary
from osm_polygon_website_tag.reporting.geographic.polygon_density import build_polygon_density_map
from osm_polygon_website_tag.reporting.geometry_stats import (
    GEOMETRY_STATS_FILENAME,
    GeometryStats,
    compute_geometry_stats,
    render_geometry_stats,
)
from osm_polygon_website_tag.reporting.text_population import compute_text_population_summary
from osm_polygon_website_tag.runtime.config import (
    DEFAULT_GITHUB_REPO,
    TRACKIO_DASHBOARD_URL,
)
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle

CARD_CONTRACT_VERSION = 2
_TOP_LEVEL_HEADING = re.compile(rb"(?m)^## [^\r\n]*(?:\r\n|\n|$)")
_WEBSITE_TEXT_HEADING = re.compile(rb"(?m)^## Website text(?:\r\n|\n|$)")
_LANGUAGE_HEADING = re.compile(rb"(?m)^## Languages(?:\r\n|\n|$)")
_GEOMETRY_HEADING = re.compile(rb"(?m)^## Polygon geometry(?:\r\n|\n|$)")
_GEOGRAPHIC_HEADING = re.compile(rb"(?m)^## Geographic distribution(?:\r\n|\n|$)")
_FRONT_MATTER = re.compile(rb"\A---(?:\r\n|\n).*?(?:\r\n|\n)---(?:\r\n|\n)?", re.DOTALL)
_RELEASE_YAML_DERIVED_KEYS = frozenset(
    {
        "language",
        "observation_count",
        "public_row_count",
        "rejection_count",
        "duplicate_count",
        "conflicting_snapshot_count",
        "sources_count",
        "expected_sources_count",
        "enriched_sources_count",
        "dataset_status",
        "website_text_success_count",
        "website_total_words",
        "contact_website_text_success_count",
        "contact_website_total_words",
        "unique_text_identity_count",
        "detected_language_count",
        "website_language_count",
        "contact_website_language_count",
        "sentence_count",
        "website_segmented_count",
        "contact_website_segmented_count",
        "polygon_density_h3_resolution",
        "polygon_density_row_count",
        "occupied_h3_cell_count",
    }
)


def build_card(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
    _yaml_source: bytes | None = None,
) -> Path:
    """Build (or rebuild) the README card for ``run_dir``.

    Returns the path to ``README.md``. The function is idempotent:
    running it twice with the same inputs produces the same output
    bytes.
    """
    run_dir = Path(run_dir)
    text_population = compute_text_population_summary(run_dir, source_names=source_names)
    summary = compute_polygon_density_summary(
        run_dir,
        source_names=source_names,
        aggregation_mode="global_unique_text",
    )
    stats = compute_card_stats(
        run_dir,
        summary=summary,
        text_population=text_population,
        source_names=source_names,
    )
    geometry = compute_geometry_stats(
        run_dir,
        source_names=source_names,
        text_population=text_population,
    )
    body = _render_markdown(
        stats, geometry=geometry, schema=_public_schema_for_card(run_dir, source_names)
    )
    front_matter = _render_yaml_front_matter(stats)
    if _yaml_source is not None:
        front_matter = _merge_yaml_custom_metadata(
            front_matter.encode("utf-8"), _yaml_source
        ).decode("utf-8")
    readme = front_matter + "\n" + body
    path = run_dir / "README.md"
    yaml_path = run_dir / "dataset.yaml"
    staged_readme = run_dir / ".README.md.building"
    staged_yaml = run_dir / ".dataset.yaml.building"
    staged_stats = run_dir / ".stats.json.building"
    staged_map = run_dir / ".assets" / "geographic_polygon_density.png.building"
    staged_map.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_polygon_density_map(
            run_dir,
            summary=summary,
            output_path=staged_map,
            source_names=source_names,
            aggregation_mode="global_unique_text",
        )
        staged_readme.write_text(readme, encoding="utf-8")
        staged_yaml.write_text(front_matter, encoding="utf-8")
        promotions = [
            (staged_map, run_dir / POLYGON_DENSITY_ASSET_REL_PATH),
            (staged_readme, path),
            (staged_yaml, yaml_path),
        ]
        promotions.extend(_staged_geometry_stats(staged_stats, run_dir, geometry))
        atomic_promote_bundle(promotions)
    finally:
        staged_map.unlink(missing_ok=True)
        staged_readme.unlink(missing_ok=True)
        staged_yaml.unlink(missing_ok=True)
        staged_stats.unlink(missing_ok=True)
    return path


def update_card_with_geometry(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> Path:
    """Add the geometry summary while preserving an existing card bundle.

    Release updates are intentionally narrower than a fresh :func:`build_card`.
    An existing README is patched only at its geometry section, and an existing
    ``dataset.yaml`` is never rewritten. A missing README falls back to the
    complete builder so legacy runs can still regenerate missing metadata.
    """
    root = Path(run_dir)
    readme = root / "README.md"
    if not readme.is_file():
        return build_card(root, source_names=source_names)

    geometry = compute_geometry_stats(root, source_names=source_names)
    original = readme.read_bytes()
    updated = _update_geometry_section(original, geometry)
    staged_readme = root / ".README.md.geometry.building"
    staged_stats = root / ".stats.json.geometry.building"
    promotions: list[tuple[Path, Path]] = []
    try:
        if updated != original:
            staged_readme.write_bytes(updated)
            promotions.append((staged_readme, readme))
        promotions.extend(_staged_geometry_stats(staged_stats, root, geometry))
        if promotions:
            atomic_promote_bundle(promotions)
    finally:
        staged_readme.unlink(missing_ok=True)
        staged_stats.unlink(missing_ok=True)
    return readme


def refresh_card_for_release(
    run_dir: Path | str,
    *,
    source_names: Collection[str] | None = None,
) -> Path:
    """Refresh every geography-bearing release artifact from one text summary.

    Existing card sections outside the derived geography and geometry blocks
    remain byte-for-byte intact. The map, README, YAML density fields, and
    geometry report are promoted together so a release cannot retain stale
    geographic metadata.
    """
    root = Path(run_dir)
    readme = root / "README.md"
    if not readme.is_file():
        yaml_path = root / "dataset.yaml"
        yaml_source = yaml_path.read_bytes() if yaml_path.is_file() else None
        return build_card(root, source_names=source_names, _yaml_source=yaml_source)

    text_population = compute_text_population_summary(root, source_names=source_names)
    summary = compute_polygon_density_summary(
        root,
        source_names=source_names,
        aggregation_mode="global_unique_text",
    )
    stats = compute_card_stats(
        root,
        summary=summary,
        text_population=text_population,
        source_names=source_names,
    )
    geometry = compute_geometry_stats(
        root,
        source_names=source_names,
        text_population=text_population,
    )
    original_readme = readme.read_bytes()
    updated_readme = _update_readme_front_matter(original_readme, stats)
    updated_readme = _update_website_text_section(updated_readme, stats)
    updated_readme = _update_language_section(updated_readme, stats)
    updated_readme = _update_geographic_section(
        _update_geometry_section(updated_readme, geometry), stats
    )
    yaml_path = root / "dataset.yaml"
    original_yaml = yaml_path.read_bytes() if yaml_path.is_file() else None
    updated_yaml = (
        _update_release_yaml(original_yaml, stats)
        if original_yaml is not None
        else _render_yaml_front_matter(stats).encode("utf-8")
    )
    _promote_release_card_artifacts(
        root,
        source_names=source_names,
        summary=summary,
        geometry=geometry,
        readme=readme,
        original_readme=original_readme,
        updated_readme=updated_readme,
        yaml_path=yaml_path,
        original_yaml=original_yaml,
        updated_yaml=updated_yaml,
    )
    return readme


def _promote_release_card_artifacts(
    root: Path,
    *,
    source_names: Collection[str] | None,
    summary: PolygonDensitySummary,
    geometry: GeometryStats,
    readme: Path,
    original_readme: bytes,
    updated_readme: bytes,
    yaml_path: Path,
    original_yaml: bytes | None,
    updated_yaml: bytes | None,
) -> None:
    """Render and atomically promote the release-derived card artifacts."""
    staged_readme = root / ".README.md.release.building"
    staged_yaml = root / ".dataset.yaml.release.building"
    staged_stats = root / ".stats.json.release.building"
    staged_map = root / ".assets" / "geographic_polygon_density.png.release.building"
    staged_map.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_polygon_density_map(
            root,
            summary=summary,
            output_path=staged_map,
            source_names=source_names,
            aggregation_mode="global_unique_text",
        )
        promotions = _release_card_promotions(
            root,
            readme=readme,
            original_readme=original_readme,
            updated_readme=updated_readme,
            yaml_path=yaml_path,
            original_yaml=original_yaml,
            updated_yaml=updated_yaml,
            staged_readme=staged_readme,
            staged_yaml=staged_yaml,
            staged_map=staged_map,
            staged_stats=staged_stats,
            geometry=geometry,
        )
        if promotions:
            atomic_promote_bundle(promotions)
    finally:
        staged_map.unlink(missing_ok=True)
        staged_readme.unlink(missing_ok=True)
        staged_yaml.unlink(missing_ok=True)
        staged_stats.unlink(missing_ok=True)


def _release_card_promotions(
    root: Path,
    *,
    readme: Path,
    original_readme: bytes,
    updated_readme: bytes,
    yaml_path: Path,
    original_yaml: bytes | None,
    updated_yaml: bytes | None,
    staged_readme: Path,
    staged_yaml: Path,
    staged_map: Path,
    staged_stats: Path,
    geometry: GeometryStats,
) -> list[tuple[Path, Path]]:
    """Stage only changed release metadata plus the derived geometry report."""
    promotions: list[tuple[Path, Path]] = []
    if updated_readme != original_readme:
        staged_readme.write_bytes(updated_readme)
        promotions.append((staged_readme, readme))
    if updated_yaml is not None and (original_yaml is None or updated_yaml != original_yaml):
        staged_yaml.write_bytes(updated_yaml)
        promotions.append((staged_yaml, yaml_path))
    promotions.extend(_stage_release_map(root, staged_map))
    promotions.extend(_staged_geometry_stats(staged_stats, root, geometry))
    return promotions


def _stage_release_map(root: Path, staged_map: Path) -> list[tuple[Path, Path]]:
    """Return a map promotion only when the rendered bytes differ."""
    target = root / POLYGON_DENSITY_ASSET_REL_PATH
    if target.is_file() and staged_map.read_bytes() == target.read_bytes():
        return []
    return [(staged_map, target)]


def _update_geometry_section(card: bytes, geometry: GeometryStats) -> bytes:
    """Replace or insert one geometry block without rewriting other card bytes."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    block = _geometry_block_bytes(geometry, newline)
    existing = _GEOMETRY_HEADING.search(card)
    if existing is not None:
        following = _TOP_LEVEL_HEADING.search(card, existing.end())
        end = following.start() if following is not None else len(card)
        return card[: existing.start()] + block + card[end:]

    insertion = _GEOGRAPHIC_HEADING.search(card)
    if insertion is not None:
        return card[: insertion.start()] + block + card[insertion.start() :]
    return _append_geometry_block(card, block, newline)


def _update_geographic_section(card: bytes, stats: CardStats) -> bytes:
    """Replace or insert the geography block using the card's newline style."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    block = newline.join(line.encode("utf-8") for line in _render_geographic_section(stats))
    block += newline
    existing = _GEOGRAPHIC_HEADING.search(card)
    if existing is not None:
        return _replace_section(card, existing, block)

    geometry = _GEOMETRY_HEADING.search(card)
    if geometry is not None:
        return _insert_after_section(card, geometry, block)
    return _append_geometry_block(card, block, newline)


def _update_website_text_section(card: bytes, stats: CardStats) -> bytes:
    """Replace the generated website-text block without adding it to legacy cards."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    existing = _WEBSITE_TEXT_HEADING.search(card)
    if existing is None:
        return card
    block = newline.join(line.encode("utf-8") for line in _render_website_text_section(stats))
    return _replace_section(card, existing, block + newline)


def _update_language_section(card: bytes, stats: CardStats) -> bytes:
    """Replace the generated language block with canonical population totals."""
    newline = b"\r\n" if b"\r\n" in card else b"\n"
    existing = _LANGUAGE_HEADING.search(card)
    if existing is not None:
        return _replace_existing_language_section(card, existing, stats, newline)
    if not stats.detected_language_count:
        return card
    return _insert_new_language_section(card, stats, newline)


def _replace_existing_language_section(
    card: bytes,
    existing: re.Match[bytes],
    stats: CardStats,
    newline: bytes,
) -> bytes:
    """Replace or remove an existing generated language section."""
    if not stats.detected_language_count:
        return _replace_section(card, existing, b"")
    return _replace_section(card, existing, _language_section_block(stats, newline))


def _insert_new_language_section(card: bytes, stats: CardStats, newline: bytes) -> bytes:
    """Insert a missing generated language section after website text."""
    website = _WEBSITE_TEXT_HEADING.search(card)
    if website is not None:
        return _insert_after_section(card, website, _language_section_block(stats, newline))
    return _append_geometry_block(card, _language_section_block(stats, newline), newline)


def _language_section_block(stats: CardStats, newline: bytes) -> bytes:
    """Render one language section using the card's newline convention."""
    return newline.join(line.encode("utf-8") for line in _render_language_section(stats)) + newline


def _replace_section(card: bytes, heading: re.Match[bytes], block: bytes) -> bytes:
    """Replace a headed card section through the next top-level heading."""
    following = _TOP_LEVEL_HEADING.search(card, heading.end())
    end = following.start() if following is not None else len(card)
    return card[: heading.start()] + block + card[end:]


def _insert_after_section(card: bytes, heading: re.Match[bytes], block: bytes) -> bytes:
    """Insert a derived section after an existing headed section."""
    following = _TOP_LEVEL_HEADING.search(card, heading.end())
    insertion = following.start() if following is not None else len(card)
    return card[:insertion] + block + card[insertion:]


def _update_density_yaml(document: bytes | None, stats: CardStats) -> bytes:
    """Update only the derived density fields in an existing dataset YAML."""
    if document is None:
        return b""
    return _update_density_yaml_text(document.decode("utf-8"), stats)


def _update_release_yaml(document: bytes | None, stats: CardStats) -> bytes:
    """Refresh generated card metrics while retaining all custom YAML fields."""
    if document is None:
        return b""
    return _update_release_yaml_text(document.decode("utf-8"), stats)


def _update_readme_front_matter(document: bytes, stats: CardStats) -> bytes:
    """Refresh existing generated README metadata without adding new fields."""
    match = _FRONT_MATTER.match(document)
    if match is None:
        return document
    updated = _update_existing_release_yaml(match.group(0), stats)
    return updated + document[match.end() :]


def _merge_yaml_custom_metadata(generated: bytes, source: bytes) -> bytes:
    """Combine trusted custom YAML with freshly generated release fields."""
    custom = "\n".join(
        line
        for line in _yaml_custom_text(source.decode("utf-8")).replace("\r\n", "\n").splitlines()
        if line.strip() != "---"
    ).strip()
    derived = "\n".join(_yaml_derived_lines(generated.decode("utf-8"))).strip()
    content = "\n".join(part for part in (custom, derived) if part)
    return f"---\n{content}\n---".encode()


def _yaml_derived_lines(document: str) -> list[str]:
    """Return generated YAML fields while retaining their list values."""
    derived: list[str] = []
    include_values = False
    for line in document.replace("\r\n", "\n").splitlines():
        key = _yaml_top_level_key(line)
        if key is not None:
            include_values = _is_derived_yaml_key(key)
            if include_values:
                derived.append(line)
        elif _is_derived_yaml_list_value(include_values, line):
            derived.append(line)
    return derived


def _is_derived_yaml_key(key: str) -> bool:
    """Return whether a top-level YAML key is release-generated."""
    return key in _RELEASE_YAML_DERIVED_KEYS


def _is_derived_yaml_list_value(include_values: bool, line: str) -> bool:
    """Return whether one continuation line belongs to a derived list."""
    return include_values and _is_yaml_list_value(line)


def _update_existing_release_yaml(document: bytes, stats: CardStats) -> bytes:
    """Replace only generated YAML fields already present in one document."""
    text = document.decode("utf-8")
    if stats.detected_language_count:
        text = _replace_release_language_tags(text, stats)
    values = {
        **_release_yaml_values(stats),
        "detected_language_count": stats.detected_language_count,
        "website_language_count": stats.website_language_count,
        "contact_website_language_count": stats.contact_website_language_count,
        "sentence_count": stats.total_sentence_count,
        "website_segmented_count": stats.website_sentence_row_count,
        "contact_website_segmented_count": stats.contact_website_sentence_row_count,
    }
    for key, value in values.items():
        text, _ = _replace_density_yaml_field(text, key, f"{key}: {value}")
    return text.encode("utf-8")


def yaml_custom_sha256(path: Path) -> str | None:
    """Hash YAML content after removing release-generated fields."""
    document = _yaml_document_bytes(path)
    if document is None:
        return None
    normalized = _yaml_custom_text(document.decode("utf-8"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _yaml_document_bytes(path: Path) -> bytes | None:
    """Return the YAML document or README front matter to hash."""
    if not path.is_file():
        return None
    document = path.read_bytes()
    if path.name == "README.md":
        match = _FRONT_MATTER.match(document)
        if match is None:
            return None
        document = match.group(0)
    return document


def _yaml_custom_text(document: str) -> str:
    """Remove release-generated top-level fields before hashing."""
    lines = document.replace("\r\n", "\n").splitlines(keepends=True)
    return "".join(_iter_yaml_custom_lines(lines)).rstrip("\n")


def _iter_yaml_custom_lines(lines: list[str]) -> Iterator[str]:
    """Yield YAML lines that belong to non-generated metadata."""
    kept: list[str] = []
    skip_language_values = False
    for line in lines:
        if skip_language_values and _is_yaml_list_value(line):
            continue
        key = _yaml_top_level_key(line)
        skip_language_values = key == "language"
        if key in _RELEASE_YAML_DERIVED_KEYS:
            continue
        kept.append(line)
    yield from kept


def _is_yaml_list_value(line: str) -> bool:
    """Return whether a line is an indented YAML list item."""
    return bool(re.match(r"^[ \t]+- ", line))


def _yaml_top_level_key(line: str) -> str | None:
    """Return a top-level YAML key, if one is present."""
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
    return match.group(1) if match else None


def _update_release_yaml_text(text: str, stats: CardStats) -> bytes:
    """Update all scalar metrics that the generated card front matter exposes."""
    text = _replace_release_language_tags(text, stats)
    required_values = _release_yaml_values(stats)
    optional_values = {
        "detected_language_count": stats.detected_language_count,
        "website_language_count": stats.website_language_count,
        "contact_website_language_count": stats.contact_website_language_count,
        "sentence_count": stats.total_sentence_count,
        "website_segmented_count": stats.website_sentence_row_count,
        "contact_website_segmented_count": stats.contact_website_sentence_row_count,
    }
    return _replace_release_yaml_fields(
        text,
        {**required_values, **optional_values},
        required_keys=required_values,
    )


def _replace_release_language_tags(text: str, stats: CardStats) -> str:
    """Refresh the generated top-level language list while preserving YAML."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    replacement = [f"{line}{newline}" for line in _language_tag_lines(stats)]
    language_range = _language_yaml_range(lines)
    if language_range is not None:
        start, end = language_range
        return "".join((*lines[:start], *replacement, *lines[end:]))
    if not replacement:
        return text
    insertion = _language_yaml_insertion_index(lines)
    return "".join((*lines[:insertion], *replacement, *lines[insertion:]))


def _language_yaml_range(lines: list[str]) -> tuple[int, int] | None:
    """Return the top-level language field range, including its list values."""
    for index, line in enumerate(lines):
        if line.startswith("language:"):
            end = index + 1
            while end < len(lines) and re.match(r"^[ \t]+- ", lines[end]):
                end += 1
            return index, end
    return None


def _language_yaml_insertion_index(lines: list[str]) -> int:
    """Return a stable insertion point for a missing language field."""
    return next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.startswith(("size_categories:", "configs:", "---"))
        ),
        len(lines),
    )


def _release_yaml_values(stats: CardStats) -> dict[str, object]:
    """Return the required scalar values exposed in generated front matter."""
    return {
        "observation_count": stats.observation_count,
        "public_row_count": stats.public_row_count,
        "rejection_count": stats.rejection_count,
        "duplicate_count": stats.duplicate_count,
        "conflicting_snapshot_count": stats.conflicting_snapshot_count,
        "sources_count": stats.sources_count,
        "expected_sources_count": stats.expected_sources_count,
        "enriched_sources_count": stats.enriched_sources_count,
        "dataset_status": _dataset_status_value(stats),
        "website_text_success_count": stats.website_text_success_count,
        "website_total_words": stats.website_total_words,
        "contact_website_text_success_count": stats.contact_website_text_success_count,
        "contact_website_total_words": stats.contact_website_total_words,
        "unique_text_identity_count": stats.polygons_with_any_text,
        "polygon_density_h3_resolution": stats.polygon_density_h3_resolution,
        "polygon_density_row_count": stats.polygon_density_row_count,
        "occupied_h3_cell_count": stats.occupied_h3_cell_count,
    }


def _replace_release_yaml_fields(
    text: str,
    values: Mapping[str, object],
    *,
    required_keys: Collection[str],
) -> bytes:
    """Replace generated fields and append missing required or nonzero fields."""
    newline = "\r\n" if "\r\n" in text else "\n"
    updated = text
    missing: list[str] = []
    for key, value in values.items():
        updated, addition = _replace_one_release_yaml_field(updated, key, value, required_keys)
        if addition is not None:
            missing.append(addition)
    if missing:
        updated = _append_density_yaml_fields(updated, missing, newline)
    return updated.encode("utf-8")


def _replace_one_release_yaml_field(
    text: str,
    key: str,
    value: object,
    required_keys: Collection[str],
) -> tuple[str, str | None]:
    """Replace one release field and return an optional missing-field addition."""
    replacement = f"{key}: {value}"
    updated, found = _replace_density_yaml_field(text, key, replacement)
    if not found and _should_append_release_yaml_field(key, value, required_keys):
        return updated, replacement
    return updated, None


def _should_append_release_yaml_field(
    key: str,
    value: object,
    required_keys: Collection[str],
) -> bool:
    """Return whether a missing generated field belongs in the document."""
    return key in required_keys or bool(value)


def _update_density_yaml_text(text: str, stats: CardStats) -> bytes:
    """Update density fields in decoded YAML while preserving other content."""
    newline = "\r\n" if "\r\n" in text else "\n"
    values = {
        "polygon_density_h3_resolution": stats.polygon_density_h3_resolution,
        "polygon_density_row_count": stats.polygon_density_row_count,
        "occupied_h3_cell_count": stats.occupied_h3_cell_count,
    }
    missing: list[str] = []
    updated = text
    for key, value in values.items():
        replacement = f"{key}: {value}"
        updated, found = _replace_density_yaml_field(updated, key, replacement)
        if not found:
            missing.append(replacement)
    if missing:
        updated = _append_density_yaml_fields(updated, missing, newline)
    return updated.encode("utf-8")


def _replace_density_yaml_field(text: str, key: str, replacement: str) -> tuple[str, bool]:
    """Replace one top-level density field and report whether it existed."""
    pattern = re.compile(rf"(?m)^{re.escape(key)}:[^\r\n]*")
    updated, count = pattern.subn(replacement, text)
    return updated, count > 0


def _append_density_yaml_fields(text: str, fields: list[str], newline: str) -> str:
    """Append missing density fields before YAML front-matter closure when present."""
    closing = f"{newline}---"
    closing_start = text.rfind(closing)
    addition = newline.join(fields) + newline
    if closing_start >= 0 and text.endswith(("---", f"---{newline}")):
        return text[:closing_start] + newline + addition + text[closing_start + len(newline) :]
    if text and not text.endswith(("\n", "\r")):
        text += newline
    return text + addition


def _geometry_block_bytes(geometry: GeometryStats, newline: bytes) -> bytes:
    """Render the additive block using the existing card's newline convention."""
    lines = _render_polygon_geometry_section(geometry)
    return newline.join(line.encode("utf-8") for line in lines) + newline


def _append_geometry_block(card: bytes, block: bytes, newline: bytes) -> bytes:
    """Append a geometry block while retaining the existing card bytes."""
    prefix = card
    if prefix and not prefix.endswith(newline):
        prefix += newline
    if prefix:
        prefix += newline
    return prefix + block


def _staged_geometry_stats(
    staged: Path,
    run_dir: Path,
    geometry: GeometryStats,
) -> list[tuple[Path, Path]]:
    """Stage ``stats.json`` only when its bytes would actually change.

    Regeneration from unchanged artifacts is a no-op: the existing file keeps
    its bytes and its metadata instead of being rewritten with identical
    content.
    """
    target = run_dir / GEOMETRY_STATS_FILENAME
    rendered = render_geometry_stats(geometry)
    if target.is_file() and target.read_text(encoding="utf-8") == rendered:
        return []
    staged.write_text(rendered, encoding="utf-8")
    return [(staged, target)]


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
        *_language_tag_lines(stats),
        "size_categories:",
        f"  - {_size_category(stats.public_row_count)}",
        "configs:",
        "  - config_name: default",
        "    data_files:",
        "      - split: polygons",
        "        path: polygons/*.parquet",
        f"observation_count: {stats.observation_count}",
        f"public_row_count: {stats.public_row_count}",
        f"rejection_count: {stats.rejection_count}",
        f"duplicate_count: {stats.duplicate_count}",
        f"conflicting_snapshot_count: {stats.conflicting_snapshot_count}",
        f"sources_count: {stats.sources_count}",
        f"expected_sources_count: {stats.expected_sources_count}",
        f"enriched_sources_count: {stats.enriched_sources_count}",
        f"dataset_status: {_dataset_status_value(stats)}",
        f"website_text_success_count: {stats.website_text_success_count}",
        f"website_total_words: {stats.website_total_words}",
        f"contact_website_text_success_count: {stats.contact_website_text_success_count}",
        f"contact_website_total_words: {stats.contact_website_total_words}",
        f"unique_text_identity_count: {stats.polygons_with_any_text}",
        *_language_metadata_lines(stats),
        *_sentence_metadata_lines(stats),
        f"polygon_density_h3_resolution: {stats.polygon_density_h3_resolution}",
        f"polygon_density_row_count: {stats.polygon_density_row_count}",
        f"occupied_h3_cell_count: {stats.occupied_h3_cell_count}",
        "---",
    ]
    return "\n".join(lines)


CARD_LANGUAGE_TAG_LIMIT = 20
CARD_TOP_LANGUAGE_LIMIT = 10


def _language_tag_lines(stats: CardStats) -> list[str]:
    """Render the Hugging Face ``language`` tags for detected labels."""
    codes = _detected_language_codes(stats)
    if not codes:
        return []
    return ["language:", *(f"  - {code}" for code in codes)]


def _detected_language_codes(stats: CardStats) -> list[str]:
    """Return deduplicated ISO 639-3 prefixes of the most frequent labels."""
    prefixes = dict.fromkeys(label.split("_")[0] for label, _ in stats.top_languages)
    return [code for code in prefixes if code][:CARD_LANGUAGE_TAG_LIMIT]


def _sentence_metadata_lines(stats: CardStats) -> list[str]:
    """Render sentence-segmentation counts for the YAML front matter."""
    if not stats.total_sentence_count:
        return []
    return [
        f"sentence_count: {stats.total_sentence_count}",
        f"website_segmented_count: {stats.website_sentence_row_count}",
        f"contact_website_segmented_count: {stats.contact_website_sentence_row_count}",
    ]


def _language_metadata_lines(stats: CardStats) -> list[str]:
    """Render detected-language counts for the YAML front matter."""
    if not stats.detected_language_count:
        return []
    return [
        f"detected_language_count: {stats.detected_language_count}",
        f"website_language_count: {stats.website_language_count}",
        f"contact_website_language_count: {stats.contact_website_language_count}",
    ]


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


def _public_schema_for_card(
    run_dir: Path, source_names: Collection[str] | None = None
) -> pa.Schema:
    """Return the richest contract the selected public artifacts actually carry."""
    paths = _selected_public_paths(run_dir, source_names)
    if _has_schema(paths, POLYGON_PUBLIC_SCHEMA_V1_5):
        return POLYGON_PUBLIC_SCHEMA_V1_5
    if _has_schema(paths, POLYGON_PUBLIC_SCHEMA_V1_4):
        return POLYGON_PUBLIC_SCHEMA_V1_4
    return POLYGON_PUBLIC_SCHEMA


def _selected_public_paths(run_dir: Path, source_names: Collection[str] | None) -> list[Path]:
    """Select public shards included in a card."""
    paths = sorted((run_dir / "polygons").glob("*.parquet"))
    if source_names is not None:
        selected = {
            f"{source_name.removesuffix('.osm.pbf')}.parquet" for source_name in source_names
        }
        paths = [path for path in paths if path.name in selected]
    return paths


def _has_schema(paths: Collection[Path], schema: pa.Schema) -> bool:
    """Return whether any selected public shard carries an exact contract."""
    return any(pq.read_schema(path).equals(schema, check_metadata=True) for path in paths)


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
