"""Contract for the sentence-segmentation fields appended by schema v1.5."""

from __future__ import annotations

import pyarrow as pa

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_4,
    POLYGON_PUBLIC_SCHEMA_V1_5,
    is_supported_public_polygon_schema,
)
from osm_polygon_website_tag.contracts.sentence_schema import (
    SENTENCE_COLUMN_NAMES,
    SENTENCE_FIELDS,
    SENTENCE_SCHEMA_VERSION,
    SENTENCE_STATUSES,
    SENTENCE_UNSUPPORTED_LANGUAGE,
)


def test_sentence_schema_version_follows_the_language_stage() -> None:
    assert SENTENCE_SCHEMA_VERSION == "v1.5"


def test_sentence_fields_cover_both_website_tags() -> None:
    """Each website tag carries its sentences, their count, and a terminal status."""
    assert SENTENCE_COLUMN_NAMES == (
        "website_sentences",
        "website_sentence_count",
        "website_sentence_status",
        "contact_website_sentences",
        "contact_website_sentence_count",
        "contact_website_sentence_status",
    )
    assert tuple(field.name for field in SENTENCE_FIELDS) == SENTENCE_COLUMN_NAMES


def test_sentences_are_a_nullable_list_of_non_null_strings() -> None:
    """A null list distinguishes 'not segmented' from an empty segmentation."""
    by_name = {field.name: field for field in SENTENCE_FIELDS}

    for prefix in ("website", "contact_website"):
        sentences = by_name[f"{prefix}_sentences"]
        assert sentences.nullable
        assert pa.types.is_list(sentences.type)
        assert pa.types.is_string(sentences.type.value_type)
        assert not sentences.type.value_field.nullable
        assert by_name[f"{prefix}_sentence_count"].type == pa.int32()
        assert by_name[f"{prefix}_sentence_count"].nullable
        assert by_name[f"{prefix}_sentence_status"].type == pa.string()
        assert not by_name[f"{prefix}_sentence_status"].nullable


def test_sentence_statuses_are_closed_and_name_the_coverage_gap() -> None:
    """A language outside the model's support is recorded, never silently null."""
    assert (
        frozenset({"success", "absent", "unsupported_language", "empty_text"}) == SENTENCE_STATUSES
    )
    assert SENTENCE_UNSUPPORTED_LANGUAGE == "unsupported_language"


def test_v1_5_appends_sentence_fields_to_v1_4_without_reordering() -> None:
    assert list(POLYGON_PUBLIC_SCHEMA_V1_5)[: len(POLYGON_PUBLIC_SCHEMA_V1_4)] == list(
        POLYGON_PUBLIC_SCHEMA_V1_4
    )
    assert list(POLYGON_PUBLIC_SCHEMA_V1_5)[len(POLYGON_PUBLIC_SCHEMA_V1_4) :] == list(
        SENTENCE_FIELDS
    )


def test_v1_5_is_accepted_for_resumption_and_migration() -> None:
    assert is_supported_public_polygon_schema(POLYGON_PUBLIC_SCHEMA_V1_5)
