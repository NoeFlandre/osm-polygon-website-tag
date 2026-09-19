"""YAML front-matter generation and preservation for dataset cards."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterator, Mapping
from pathlib import Path

from osm_polygon_website_tag.reporting.card_stats import CardStats

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

CARD_LANGUAGE_TAG_LIMIT = 20
_README_HEADING = re.compile(rb"(?m)^## [^\r\n]*(?:\r\n|\n|$)")
_DERIVED_README_HEADINGS = frozenset(
    {
        b"## Website text",
        b"## Languages",
        b"## Polygon geometry",
        b"## Geographic distribution",
    }
)


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
    if _should_replace_existing_languages(text, stats):
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


def _should_replace_existing_languages(text: str, stats: CardStats) -> bool:
    """Return whether a README's existing language list is release-generated."""
    language_range = _language_yaml_range(text.splitlines(keepends=True))
    has_language_metrics = any(
        f"{key}:" in text for key in ("detected_language_count", "website_language_count")
    )
    return (
        language_range is not None
        and language_range[1] > language_range[0] + 1
        and (has_language_metrics or bool(stats.detected_language_count))
    )


def yaml_custom_sha256(path: Path) -> str | None:
    """Hash YAML content after removing release-generated fields."""
    document = _yaml_document_bytes(path)
    if document is None:
        return None
    return yaml_custom_sha256_bytes(document, readme=path.name == "README.md")


def yaml_custom_sha256_bytes(document: bytes, *, readme: bool = False) -> str | None:
    """Hash one already-read YAML document after removing generated fields."""
    if readme:
        match = _FRONT_MATTER.match(document)
        if match is None:
            return None
        document = match.group(0)
    normalized = _yaml_custom_text(document.decode("utf-8"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def readme_preserved_sha256(path: Path) -> str | None:
    """Hash README body sections that a release refresh must preserve."""
    if not path.is_file():
        return None
    return readme_preserved_sha256_bytes(path.read_bytes())


def readme_preserved_sha256_bytes(document: bytes) -> str:
    """Hash README body bytes after removing release-derived sections."""
    match = _FRONT_MATTER.match(document)
    body = document[match.end() :] if match is not None else document
    preserved: list[bytes] = []
    cursor = 0
    headings = list(_README_HEADING.finditer(body))
    for index, heading in enumerate(headings):
        title = heading.group(0).rstrip(b"\r\n")
        if title not in _DERIVED_README_HEADINGS:
            continue
        preserved.append(body[cursor : heading.start()])
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        cursor = end
    preserved.append(body[cursor:])
    return hashlib.sha256(b"".join(preserved)).hexdigest()


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
