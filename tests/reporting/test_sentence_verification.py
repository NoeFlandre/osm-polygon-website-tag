"""Tests for v1.5 sentence-field invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.reporting.verification import sentence as sentence_module
from osm_polygon_website_tag.reporting.verification.sentence import verify_sentence_invariants

_SCHEMA = pa.schema(
    [
        pa.field("website_text_status", pa.string()),
        pa.field("website_language", pa.string()),
        pa.field("website_sentences", pa.list_(pa.field("element", pa.string(), nullable=False))),
        pa.field("website_sentence_count", pa.int32()),
        pa.field("website_sentence_status", pa.string()),
        pa.field("contact_website_text_status", pa.string()),
        pa.field("contact_website_language", pa.string()),
        pa.field(
            "contact_website_sentences", pa.list_(pa.field("element", pa.string(), nullable=False))
        ),
        pa.field("contact_website_sentence_count", pa.int32()),
        pa.field("contact_website_sentence_status", pa.string()),
    ]
)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "website_text_status": "success",
        "website_language": "eng_Latn",
        "website_sentences": ["One. ", "Two."],
        "website_sentence_count": 2,
        "website_sentence_status": "success",
        "contact_website_text_status": "absent",
        "contact_website_language": None,
        "contact_website_sentences": None,
        "contact_website_sentence_count": None,
        "contact_website_sentence_status": "absent",
    }
    row.update(overrides)
    return row


def _write(tmp_path: Path, rows: list[dict[str, Any]], *, schema: pa.Schema = _SCHEMA) -> Path:
    path = tmp_path / "polygons" / "source.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return path


def _verify(tmp_path: Path) -> list[str]:
    errors: list[str] = []
    verify_sentence_invariants(tmp_path, errors)
    return errors


def test_a_consistent_v1_5_shard_reports_no_errors(tmp_path: Path) -> None:
    _write(tmp_path, [_row()])

    assert _verify(tmp_path) == []


def test_a_v1_4_shard_is_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [{"website_text_status": "success", "website_language": "eng_Latn"}],
        schema=pa.schema(
            [
                pa.field("website_text_status", pa.string()),
                pa.field("website_language", pa.string()),
            ]
        ),
    )

    assert _verify(tmp_path) == []


def test_an_unknown_status_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, [_row(website_sentence_status="segmented")])

    errors = _verify(tmp_path)

    assert len(errors) == 1
    assert "unknown sentence status 'segmented'" in errors[0]
    assert "row 0 website" in errors[0]


def test_a_successful_row_must_carry_counted_sentences(tmp_path: Path) -> None:
    _write(tmp_path, [_row(website_sentences=None, website_sentence_count=None)])

    errors = _verify(tmp_path)

    assert any("sentences are missing" in error for error in errors)


def test_a_successful_row_must_not_be_empty(tmp_path: Path) -> None:
    _write(tmp_path, [_row(website_sentences=[], website_sentence_count=0)])

    errors = _verify(tmp_path)

    assert any("sentences are missing" in error for error in errors)


def test_a_count_that_disagrees_with_the_sentences_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, [_row(website_sentence_count=3)])

    errors = _verify(tmp_path)

    assert any("sentence count 3 does not match 2 sentences" in error for error in errors)


def test_an_unsuccessful_row_must_not_carry_sentences(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            _row(
                website_sentence_status="unsupported_language",
                website_sentences=["One."],
                website_sentence_count=1,
            )
        ],
    )

    errors = _verify(tmp_path)

    assert any("sentence fields must be null" in error for error in errors)


def test_segmentation_requires_successfully_extracted_text(tmp_path: Path) -> None:
    _write(tmp_path, [_row(website_text_status="fetch_error")])

    errors = _verify(tmp_path)

    assert any("segmented text that was not extracted" in error for error in errors)


def test_absent_text_must_record_absent_sentences(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            _row(
                website_text_status="absent",
                website_sentences=None,
                website_sentence_count=None,
                website_sentence_status="empty_text",
            )
        ],
    )

    errors = _verify(tmp_path)

    assert any("must record absent sentences" in error for error in errors)


def test_an_uncovered_language_must_not_be_segmented(tmp_path: Path) -> None:
    _write(tmp_path, [_row(website_language="hrv_Latn")])

    errors = _verify(tmp_path)

    assert any("language 'hrv_Latn' the segmenter does not cover" in error for error in errors)


def test_a_covered_language_must_not_be_recorded_unsupported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            _row(
                website_sentence_status="unsupported_language",
                website_sentences=None,
                website_sentence_count=None,
            )
        ],
    )

    errors = _verify(tmp_path)

    assert any("covered language 'eng_Latn'" in error for error in errors)


def test_both_website_fields_are_verified(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            _row(
                contact_website_text_status="success",
                contact_website_language="fra_Latn",
                contact_website_sentence_status="success",
                contact_website_sentences=None,
                contact_website_sentence_count=None,
            )
        ],
    )

    errors = _verify(tmp_path)

    assert any("contact_website sentences are missing" in error for error in errors)


def test_an_unreadable_shard_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "polygons" / "broken.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not parquet")

    errors = _verify(tmp_path)

    assert len(errors) == 1
    assert "unreadable sentence shard" in errors[0]


def test_a_shard_that_fails_mid_read_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, [_row()])

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("truncated")

    monkeypatch.setattr(sentence_module, "_verify_sentence_shard", explode)

    errors = _verify(tmp_path)

    assert len(errors) == 1
    assert "sentence invariant verification failed" in errors[0]
    assert "truncated" in errors[0]


def test_every_row_of_a_large_shard_is_verified(tmp_path: Path) -> None:
    rows = [_row() for _ in range(sentence_module._SENTENCE_BATCH_ROWS + 3)]
    rows[-1] = _row(website_sentence_count=9)
    path = _write(tmp_path, rows)

    errors = _verify(tmp_path)

    assert errors == [
        f"{path} row {len(rows) - 1} website sentence count 9 does not match 2 sentences"
    ]


def _field(**overrides: Any) -> list[str]:
    """Verify one website field directly and return the errors it produced."""
    errors: list[str] = []
    arguments: dict[str, Any] = {
        "location": "shard row 0 website",
        "text_status": "success",
        "language": "eng_Latn",
        "sentences": ["One."],
        "count": 1,
        "status": "success",
    }
    arguments.update(overrides)
    sentence_module._verify_sentence_field(errors=errors, **arguments)
    return errors


def test_field_verification_accepts_every_documented_status() -> None:
    assert _field() == []
    assert _field(text_status="absent", status="absent", sentences=None, count=None) == []
    assert (
        _field(language="hrv_Latn", status="unsupported_language", sentences=None, count=None) == []
    )
    assert _field(status="empty_text", sentences=None, count=None) == []


def test_field_verification_reports_provenance_against_its_location() -> None:
    assert _field(language="hrv_Latn") == [
        "shard row 0 website segmented a language 'hrv_Latn' the segmenter does not cover"
    ]
    assert _field(text_status="absent", status="empty_text", sentences=None, count=None) == [
        "shard row 0 website absent text must record absent sentences"
    ]


def test_field_verification_reports_an_unknown_status_once() -> None:
    assert _field(status="segmented") == ["shard row 0 website unknown sentence status 'segmented'"]
    assert _field(status=None) == ["shard row 0 website unknown sentence status None"]


def test_value_verification_pins_its_messages() -> None:
    errors: list[str] = []

    sentence_module._verify_sentence_values("loc", "success", None, None, errors)
    sentence_module._verify_sentence_values("loc", "success", [], 0, errors)
    sentence_module._verify_sentence_values("loc", "success", ["a"], 2, errors)
    sentence_module._verify_sentence_values("loc", "absent", ["a"], 1, errors)
    sentence_module._verify_sentence_values("loc", "absent", None, 1, errors)
    sentence_module._verify_sentence_values("loc", "absent", None, None, errors)

    assert errors == [
        "loc sentences are missing",
        "loc sentences are missing",
        "loc sentence count 2 does not match 1 sentences",
        "loc sentence fields must be null when text was not segmented",
        "loc sentence fields must be null when text was not segmented",
    ]


def test_segmented_value_verification_rejects_a_non_list() -> None:
    errors: list[str] = []

    sentence_module._verify_segmented_values("loc", "One.", 1, errors)

    assert errors == ["loc sentences are missing"]


def test_language_coverage_verification_pins_its_messages() -> None:
    errors: list[str] = []

    sentence_module._verify_language_coverage("loc", "success", "hrv_Latn", errors)
    sentence_module._verify_language_coverage("loc", "unsupported_language", "eng_Latn", errors)
    sentence_module._verify_language_coverage("loc", "success", "eng_Latn", errors)
    sentence_module._verify_language_coverage("loc", "unsupported_language", "hrv_Latn", errors)
    sentence_module._verify_language_coverage("loc", "empty_text", None, errors)

    assert errors == [
        "loc segmented a language 'hrv_Latn' the segmenter does not cover",
        "loc rejected a covered language 'eng_Latn'",
    ]


def test_unfetched_text_verification_pins_its_messages() -> None:
    errors: list[str] = []

    sentence_module._verify_unfetched_text("loc", "success", "fetch_error", errors)
    sentence_module._verify_unfetched_text("loc", "empty_text", "absent", errors)
    sentence_module._verify_unfetched_text("loc", "absent", "absent", errors)
    sentence_module._verify_unfetched_text("loc", "empty_text", "fetch_error", errors)

    assert errors == [
        "loc segmented text that was not extracted",
        "loc absent text must record absent sentences",
    ]


def test_provenance_verification_routes_by_text_status() -> None:
    unfetched: list[str] = []
    fetched: list[str] = []
    absent: list[str] = []

    sentence_module._verify_sentence_provenance(
        "loc", "success", "fetch_error", "eng_Latn", unfetched
    )
    sentence_module._verify_sentence_provenance("loc", "success", "success", "hrv_Latn", fetched)
    sentence_module._verify_sentence_provenance("loc", "empty_text", "absent", None, absent)

    assert unfetched == ["loc segmented text that was not extracted"]
    assert fetched == ["loc segmented a language 'hrv_Latn' the segmenter does not cover"]
    assert absent == ["loc absent text must record absent sentences"]


def test_batch_verification_reads_both_prefixes_in_column_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            _row(),
            _row(
                website_sentence_count=5,
                contact_website_text_status="success",
                contact_website_language="fra_Latn",
                contact_website_sentence_status="success",
                contact_website_sentences=None,
                contact_website_sentence_count=None,
            ),
        ],
    )
    batch = next(pq.ParquetFile(path).iter_batches(columns=list(sentence_module._SENTENCE_COLUMNS)))
    errors: list[str] = []

    sentence_module._verify_sentence_batch(Path("shard"), 1, batch, errors)

    assert errors == [
        "shard row 513 website sentence count 5 does not match 2 sentences",
        "shard row 513 contact_website sentences are missing",
    ]


def test_shard_verification_reads_the_contract_columns_from_a_wider_shard(
    tmp_path: Path,
) -> None:
    """A shard carries many columns; reading all of them would misalign rows."""
    wide_schema = pa.schema(
        [pa.field("polygon_id", pa.string()), *list(_SCHEMA), pa.field("region", pa.string())]
    )
    rows = [{**_row(website_sentence_count=7), "polygon_id": "way/1", "region": "bayern"}]
    path = tmp_path / "polygons" / "wide.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=wide_schema), path)
    errors: list[str] = []

    sentence_module._verify_sentence_shard(path, errors)

    assert errors == [
        f"{path} row 0 website sentence count 7 does not match 2 sentences",
    ]


def test_shard_verification_reads_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, [_row() for _ in range(sentence_module._SENTENCE_BATCH_ROWS + 1)])
    sizes: list[int] = []
    monkeypatch.setattr(
        sentence_module,
        "_verify_sentence_batch",
        lambda _path, _number, batch, _errors: sizes.append(batch.num_rows),
    )

    sentence_module._verify_sentence_shard(path, [])

    assert sizes == [sentence_module._SENTENCE_BATCH_ROWS, 1]


def test_sentence_columns_match_the_published_contract() -> None:
    assert sentence_module._SENTENCE_COLUMNS == (
        "website_text_status",
        "website_language",
        "website_sentences",
        "website_sentence_count",
        "website_sentence_status",
        "contact_website_text_status",
        "contact_website_language",
        "contact_website_sentences",
        "contact_website_sentence_count",
        "contact_website_sentence_status",
    )
    assert sentence_module._FIELDS_PER_PREFIX == 5
    assert sentence_module._SENTENCE_BATCH_ROWS == 512
