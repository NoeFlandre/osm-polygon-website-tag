"""Contract tests for card YAML preservation and refresh helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.fixtures.card import _golden_card_stats, _sentence_card_stats

import osm_polygon_website_tag.reporting.card_metadata as metadata
from osm_polygon_website_tag.reporting.card_stats import CardStats


def test_merge_yaml_custom_metadata_preserves_custom_fields_and_derived_lists() -> None:
    merged = metadata._merge_yaml_custom_metadata(
        b"---\r\nlanguage:\r\n  - eng\r\nsize_categories:\r\n  - n<1K\r\n"
        b"website_text_success_count: 3\r\n---\r\n",
        b"---\r\nlicense: odbl\r\ncustom_field: keep\r\nlanguage:\r\n  - old\r\n---\r\n",
    )

    assert merged == (
        b"---\n"
        b"license: odbl\n"
        b"custom_field: keep\n"
        b"language:\n"
        b"  - eng\n"
        b"website_text_success_count: 3\n"
        b"---"
    )


def test_yaml_derived_lines_only_include_derived_fields_and_their_lists() -> None:
    document = (
        "  - orphan\n"
        "language:\n"
        "  - eng\n"
        "size_categories:\n"
        "  - n<1K\n"
        "website_text_success_count: 3\n"
        "custom_list:\n"
        "  - keep\n"
    )

    assert metadata._yaml_derived_lines(document) == [
        "language:",
        "  - eng",
        "website_text_success_count: 3",
    ]
    assert metadata._yaml_derived_lines(document.replace("\n", "\r\n")) == [
        "language:",
        "  - eng",
        "website_text_success_count: 3",
    ]


def test_yaml_wrappers_decode_existing_documents_and_handle_missing_documents() -> None:
    stats = _golden_card_stats()

    assert metadata._update_density_yaml(None, stats) == b""
    assert metadata._update_release_yaml(None, stats) == b""
    assert b"polygon_density_row_count: 22" in metadata._update_density_yaml(
        b"license: odbl\n---\n", stats
    )
    assert b"observation_count: 2" in metadata._update_release_yaml(b"license: odbl\n---\n", stats)


def test_update_existing_release_yaml_refreshes_every_optional_metric() -> None:
    stats = _sentence_card_stats()
    document = "\n".join(
        [
            "language:",
            "  - old",
            "detected_language_count: 1",
            "website_language_count: 1",
            "contact_website_language_count: 1",
            "sentence_count: 1",
            "website_segmented_count: 1",
            "contact_website_segmented_count: 1",
            "---",
        ]
    ).encode()

    updated = metadata._update_existing_release_yaml(document, stats).decode()

    assert "  - old" not in updated
    assert "  - eng\n  - deu" in updated
    assert "detected_language_count: 2" in updated
    assert "website_language_count: 10" in updated
    assert "contact_website_language_count: 15" in updated
    assert "sentence_count: 60" in updated
    assert "website_segmented_count: 8" in updated
    assert "contact_website_segmented_count: 4" in updated


def test_should_replace_existing_languages_requires_a_real_generated_list() -> None:
    no_values = "language:\nlicense: odbl\n"
    one_value = "language:\n  - eng\nlicense: odbl\n"
    generated = "language:\n  - eng\n  - deu\nlicense: odbl\n"

    assert not metadata._should_replace_existing_languages(no_values, CardStats())
    assert not metadata._should_replace_existing_languages(
        no_values.replace("license: odbl", "website_language_count: 2"), CardStats()
    )
    assert not metadata._should_replace_existing_languages(one_value, CardStats())
    assert metadata._should_replace_existing_languages(generated, CardStats()) is False
    assert metadata._should_replace_existing_languages(
        one_value.replace("license: odbl", "website_language_count: 2"), CardStats()
    )
    assert metadata._should_replace_existing_languages(
        generated.replace("license: odbl", "website_language_count: 2"), CardStats()
    )
    assert metadata._should_replace_existing_languages(
        generated, CardStats(detected_language_count=1)
    )


def test_yaml_document_selection_distinguishes_readmes_and_yaml_files(tmp_path: Path) -> None:
    front_matter = b"---\nlicense: odbl\n---\n"
    readme = tmp_path / "README.md"
    uppercase_readme = tmp_path / "README.MD"
    dataset_yaml = tmp_path / "dataset.yaml"
    invalid_readme_dir = tmp_path / "invalid"
    invalid_readme_dir.mkdir()
    missing_front_matter = invalid_readme_dir / "README.md"
    readme.write_bytes(front_matter + b"# body\n")
    uppercase_readme.write_bytes(front_matter + b"# body\n")
    dataset_yaml.write_bytes(front_matter + b"# body\n")
    missing_front_matter.write_bytes(b"# body\n")

    assert metadata._yaml_document_bytes(readme) == front_matter
    assert metadata._yaml_document_bytes(uppercase_readme) == front_matter + b"# body\n"
    assert metadata._yaml_document_bytes(dataset_yaml) == front_matter + b"# body\n"
    assert metadata._yaml_document_bytes(missing_front_matter) is None
    assert metadata._yaml_document_bytes(tmp_path / "absent.yaml") is None
    assert metadata.yaml_custom_sha256(dataset_yaml) != metadata.yaml_custom_sha256(readme)


def test_yaml_custom_text_removes_only_generated_fields_and_language_values() -> None:
    document = (
        "  - orphan\r\n"
        "tags:\r\n"
        "  - keep-tag\r\n"
        "language:\r\n"
        "  - eng\r\n"
        "custom_list:\r\n"
        "  - keep-value\r\n"
        "website_text_success_count: 4\r\n"
        "custom_scalar: keepX \r\n"
    )

    assert metadata._yaml_custom_text(document) == (
        "  - orphan\ntags:\n  - keep-tag\ncustom_list:\n  - keep-value\ncustom_scalar: keepX "
    )
    assert metadata._yaml_custom_text("custom_scalar: keep \n") == "custom_scalar: keep "
    assert metadata._yaml_custom_text("custom_scalar: keepX\n") == "custom_scalar: keepX"


def test_yaml_custom_text_skips_only_values_belonging_to_language_key() -> None:
    assert metadata._yaml_custom_text("language:\n  - eng\nlicense: odbl\n") == ("license: odbl")


def test_yaml_line_classifiers_cover_case_and_indentation_boundaries() -> None:
    assert metadata._is_yaml_list_value("  - item\n")
    assert not metadata._is_yaml_list_value("- item\n")
    assert metadata._yaml_top_level_key("License: odbl") == "License"
    assert metadata._yaml_top_level_key(" nested: value") is None
    assert metadata._yaml_top_level_key("not-a-key: value") is None


def test_readme_preserved_hash_excludes_all_generated_sections_including_last() -> None:
    document = (
        b"---\nlicense: odbl\n---\n"
        b"# Title\n"
        b"## Website text\nremove\n"
        b"## Custom notes\nkeep\n"
        b"## Polygon geometry\nremove\n"
        b"## Custom tail\nkeep-tail\n"
        b"## Geographic distribution\nremove-last\n"
    )
    preserved = b"# Title\n## Custom notes\nkeep\n## Custom tail\nkeep-tail\n"

    assert metadata.readme_preserved_sha256_bytes(document) == hashlib.sha256(preserved).hexdigest()

    spaced_heading = b"---\nlicense: odbl\n---\n## Website text  \nkeep\n"
    suffixed_heading = b"---\nlicense: odbl\n---\n## Website textX\nkeep\n"
    assert (
        metadata.readme_preserved_sha256_bytes(spaced_heading)
        == hashlib.sha256(spaced_heading.split(b"---\n", 2)[-1]).hexdigest()
    )
    assert (
        metadata.readme_preserved_sha256_bytes(suffixed_heading)
        == hashlib.sha256(suffixed_heading.split(b"---\n", 2)[-1]).hexdigest()
    )
    trailing_custom = (
        b"---\nlicense: odbl\n---\n## Polygon geometry\nremove\n## Custom tail\nkeep\n"
    )
    assert (
        metadata.readme_preserved_sha256_bytes(trailing_custom)
        == hashlib.sha256(b"## Custom tail\nkeep\n").hexdigest()
    )


def test_readme_preserved_hash_starts_preservation_cursor_at_body_start() -> None:
    document = b"---\nlicense: odbl\n---\nintro\n## Website text\nderived\n"

    assert (
        metadata.readme_preserved_sha256_bytes(document) == hashlib.sha256(b"intro\n").hexdigest()
    )


def test_release_yaml_text_preserves_crlf_and_omits_zero_optional_fields() -> None:
    updated = metadata._update_release_yaml_text(
        "license: odbl\r\n---\r\n", _golden_card_stats()
    ).decode()

    assert "\r\n" in updated
    assert "\n" not in updated.replace("\r\n", "")
    assert "observation_count: 2\r\n" in updated
    assert "polygon_density_row_count: 22\r\n" in updated
    assert "detected_language_count:" not in updated
    assert updated.endswith("---\r\n")


def test_release_yaml_text_appends_nonzero_optional_fields_and_refreshes_crlf_languages() -> None:
    updated = metadata._update_release_yaml_text(
        "language:\r\n  - old\r\nsize_categories:\r\n  - n<1K\r\n---\r\n",
        _sentence_card_stats(),
    ).decode()

    assert "language:\r\n  - eng\r\n  - deu\r\n" in updated
    assert "  - old\r\n" not in updated
    assert "detected_language_count: 2\r\n" in updated
    assert "website_language_count: 10\r\n" in updated
    assert "contact_website_language_count: 15\r\n" in updated
    assert "sentence_count: 60\r\n" in updated
    assert "website_segmented_count: 8\r\n" in updated
    assert "contact_website_segmented_count: 4\r\n" in updated
    assert "\n" not in updated.replace("\r\n", "")


def test_language_yaml_ranges_and_insertion_points_are_stable() -> None:
    lines = [
        "---\n",
        "language:\n",
        "  - eng\n",
        "  - deu\n",
        "size_categories:\n",
    ]

    assert metadata._language_yaml_range(lines) == (1, 4)
    assert metadata._language_yaml_range(["language:\n", "size_categories:\n"]) == (0, 1)
    assert metadata._language_yaml_range(["---\n", "license: odbl\n"]) is None
    assert (
        metadata._language_yaml_insertion_index(["---\n", "license: odbl\n", "size_categories:\n"])
        == 2
    )
    assert metadata._language_yaml_insertion_index(["---\n", "license: odbl\n", "configs:\n"]) == 2
    assert metadata._language_yaml_insertion_index(["---\n", "license: odbl\n"]) == 2
    assert metadata._language_yaml_insertion_index(["---\n", "license: odbl\n", "---\n"]) == 2


def test_release_field_append_policy_distinguishes_required_and_nonzero_optional_values() -> None:
    assert metadata._should_append_release_yaml_field("required", 0, {"required"})
    assert metadata._should_append_release_yaml_field("optional", 3, set())
    assert not metadata._should_append_release_yaml_field("optional", 0, set())

    assert metadata._replace_one_release_yaml_field(
        "license: odbl\n---\n", "optional", 0, set()
    ) == ("license: odbl\n---\n", None)
    assert metadata._replace_one_release_yaml_field(
        "license: odbl\n---\n", "optional", 3, set()
    ) == ("license: odbl\n---\n", "optional: 3")


def test_append_density_fields_handles_closures_and_trailing_newlines() -> None:
    assert (
        metadata._append_density_yaml_fields("license: odbl\n---\n", ["density: 1"], "\n")
        == "license: odbl\ndensity: 1\n---\n"
    )
    assert (
        metadata._append_density_yaml_fields("license: odbl\r\n---\r\n", ["density: 1"], "\r\n")
        == "license: odbl\r\ndensity: 1\r\n---\r\n"
    )
    assert metadata._append_density_yaml_fields("", ["density: 1"], "\n") == "density: 1\n"
    assert metadata._append_density_yaml_fields("prefix\n", ["density: 1"], "\n") == (
        "prefix\ndensity: 1\n"
    )
    assert metadata._append_density_yaml_fields("prefix\r", ["density: 1"], "\n") == (
        "prefix\rdensity: 1\n"
    )
    assert metadata._append_density_yaml_fields("\n---", ["density: 1"], "\n") == (
        "\ndensity: 1\n---"
    )
    assert metadata._append_density_yaml_fields("prefix\n---\nbody", ["density: 1"], "\n") == (
        "prefix\n---\nbody\ndensity: 1\n"
    )
    assert (
        metadata._append_density_yaml_fields("prefix\n---\nbody\n---", ["density: 1"], "\n")
        == "prefix\n---\nbody\ndensity: 1\n---"
    )


def test_update_density_yaml_text_handles_crlf_and_non_front_matter_documents() -> None:
    stats = CardStats(
        polygon_density_h3_resolution=3,
        polygon_density_row_count=7,
        occupied_h3_cell_count=5,
    )

    updated = metadata._update_density_yaml_text("license: odbl\r\n---\r\n", stats).decode()
    appended = metadata._update_density_yaml_text("prefix", stats).decode()

    assert "polygon_density_h3_resolution: 3\r\n" in updated
    assert "polygon_density_row_count: 7\r\n" in updated
    assert "occupied_h3_cell_count: 5\r\n" in updated
    assert "\n" not in updated.replace("\r\n", "")
    assert appended.startswith("prefix\npolygon_density_h3_resolution: 3\n")
