"""Behavior of website text verification over real Parquet shards."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.reporting.verification.text import (
    verify_text_invariants,
    verify_text_paths,
)

TEXT_SCHEMA = pa.schema(
    [
        pa.field("website", pa.string()),
        pa.field("contact_website", pa.string()),
        pa.field("website_text", pa.string()),
        pa.field("website_word_count", pa.int64()),
        pa.field("website_text_status", pa.string()),
        pa.field("contact_website_text", pa.string()),
        pa.field("contact_website_word_count", pa.int64()),
        pa.field("contact_website_text_status", pa.string()),
    ]
)

URL = "https://example.org"


def _row(
    *,
    website: str | None = URL,
    text: str | None = "one two",
    word_count: int | None = 2,
    status: str | None = "success",
) -> dict[str, object]:
    return {
        "website": website,
        "website_text": text,
        "website_word_count": word_count,
        "website_text_status": status,
        "contact_website": None,
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _shard(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=TEXT_SCHEMA), path)
    return path


def _errors(
    rows: list[dict[str, object]], tmp_path: Path, status: object = "complete"
) -> list[str]:
    errors: list[str] = []
    verify_text_paths([_shard(tmp_path / "a.parquet", rows)], status, errors)
    return errors


def test_consistent_rows_pass(tmp_path: Path) -> None:
    rows = [
        _row(),
        _row(text="", word_count=0, status="empty"),
        _row(text=None, word_count=None, status="fetch_error"),
        _row(website=None, text=None, word_count=None, status="absent"),
    ]

    assert _errors(rows, tmp_path) == []


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_row(status="bogus"), "a.parquet:website has invalid text status"),
        (_row(status=None), "a.parquet:website has invalid text status"),
        (
            _row(website=None, text="x", word_count=1, status="absent"),
            "a.parquet:website absent tag has inconsistent text fields",
        ),
        (
            _row(website=None, text=None, word_count=None, status="fetch_error"),
            "a.parquet:website absent tag has inconsistent text fields",
        ),
        (
            _row(website=None, text=None, word_count=1, status="absent"),
            "a.parquet:website absent tag has inconsistent text fields",
        ),
        (
            _row(text=None, word_count=None, status="absent"),
            "a.parquet:website present tag has absent text status",
        ),
        (_row(text=None, word_count=2), "a.parquet:website success has no text"),
        (_row(word_count=3), "a.parquet:website word count does not match stored text"),
        (_row(word_count=None), "a.parquet:website word count does not match stored text"),
        (
            _row(text=None, word_count=0, status="empty"),
            "a.parquet:website empty result has inconsistent text fields",
        ),
        (
            _row(text="", word_count=1, status="empty"),
            "a.parquet:website empty result has inconsistent text fields",
        ),
        (
            _row(text="x", word_count=None, status="fetch_error"),
            "a.parquet:website non-success status must have null text and word count",
        ),
        (
            _row(text=None, word_count=1, status="fetch_error"),
            "a.parquet:website non-success status must have null text and word count",
        ),
    ],
)
def test_inconsistent_rows_are_reported(
    tmp_path: Path, row: dict[str, object], message: str
) -> None:
    assert _errors([row], tmp_path) == [message]


def test_contact_website_is_checked_with_its_own_label(tmp_path: Path) -> None:
    row = _row()
    row["contact_website_text"] = "stray"

    assert _errors([row], tmp_path) == [
        "a.parquet:contact_website absent tag has inconsistent text fields"
    ]


@pytest.mark.parametrize("status", ["enriched", "analyzed", "card_built", "verified", "complete"])
def test_pending_text_is_rejected_once_the_run_is_enriched(tmp_path: Path, status: str) -> None:
    row = _row(text=None, word_count=None, status="pending")

    assert _errors([row], tmp_path, status) == [
        "a.parquet:website remains pending after enrichment"
    ]


@pytest.mark.parametrize("status", ["extracted", None])
def test_pending_text_is_allowed_before_enrichment(tmp_path: Path, status: object) -> None:
    row = _row(text=None, word_count=None, status="pending")

    assert _errors([row], tmp_path, status) == []


def test_rows_are_read_across_batches(tmp_path: Path) -> None:
    rows = [_row() for _ in range(600)] + [_row(word_count=9)]

    assert _errors(rows, tmp_path) == ["a.parquet:website word count does not match stored text"]


def test_unreadable_shard_is_reported(tmp_path: Path) -> None:
    broken = tmp_path / "broken.parquet"
    broken.write_bytes(b"not parquet")
    errors: list[str] = []

    verify_text_paths([broken], "complete", errors)

    assert len(errors) == 1
    assert errors[0].startswith("text invariant verification failed for broken.parquet: ")


def test_invariants_scan_public_shards_in_sorted_order(tmp_path: Path) -> None:
    bad = _row(word_count=9)
    _shard(tmp_path / "polygons" / "b.parquet", [bad])
    _shard(tmp_path / "polygons" / "a.parquet", [bad])
    _shard(tmp_path / "other" / "c.parquet", [bad])
    errors: list[str] = []

    verify_text_invariants(tmp_path, "complete", errors)

    assert errors == [
        "a.parquet:website word count does not match stored text",
        "b.parquet:website word count does not match stored text",
    ]


def test_invariants_reject_pending_text_once_the_run_is_enriched(tmp_path: Path) -> None:
    _shard(
        tmp_path / "polygons" / "a.parquet", [_row(text=None, word_count=None, status="pending")]
    )
    errors: list[str] = []

    verify_text_invariants(tmp_path, "enriched", errors)

    assert errors == ["a.parquet:website remains pending after enrichment"]


def test_pending_contact_website_is_rejected_once_the_run_is_enriched(tmp_path: Path) -> None:
    row = _row()
    row["contact_website"] = URL
    row["contact_website_text_status"] = "pending"

    assert _errors([row], tmp_path) == [
        "a.parquet:contact_website remains pending after enrichment"
    ]


def test_boolean_word_counts_are_rejected(tmp_path: Path) -> None:
    schema = TEXT_SCHEMA.set(
        TEXT_SCHEMA.get_field_index("website_word_count"),
        pa.field("website_word_count", pa.bool_()),
    )
    path = tmp_path / "a.parquet"
    pq.write_table(pa.Table.from_pylist([_row(text="one", word_count=True)], schema=schema), path)
    errors: list[str] = []

    verify_text_paths([path], "complete", errors)

    assert errors == ["a.parquet:website word count does not match stored text"]
