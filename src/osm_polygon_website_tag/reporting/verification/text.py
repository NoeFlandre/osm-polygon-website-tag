"""Validation of website text enrichment fields."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.text_schema import TEXT_STATUSES, count_words


def verify_text_invariants(root: Path, status: object, errors: list[str]) -> None:
    """Verify text/status relationships in public polygon shards."""
    pending_forbidden = status in {
        "enriched",
        "analyzed",
        "card_built",
        "verified",
        "complete",
    }
    for shard in sorted((root / "polygons").glob("*.parquet")):
        _verify_text_shard(shard, pending_forbidden, errors)


def _verify_text_shard(shard: Path, pending_forbidden: bool, errors: list[str]) -> None:
    try:
        parquet = pq.ParquetFile(shard)
        columns = [
            "website",
            "contact_website",
            "website_text",
            "website_word_count",
            "website_text_status",
            "contact_website_text",
            "contact_website_word_count",
            "contact_website_text_status",
        ]
        for batch in parquet.iter_batches(columns=columns, batch_size=512):
            for row in batch.to_pylist():
                _verify_text_row(row, shard.name, pending_forbidden, errors)
    except Exception as exc:
        errors.append(f"text invariant verification failed for {shard.name}: {exc}")


def _verify_text_row(
    row: dict[str, object],
    shard_name: str,
    pending_forbidden: bool,
    errors: list[str],
) -> None:
    _verify_one_text_value(
        tag_value=row["website"],
        text=row["website_text"],
        word_count=row["website_word_count"],
        text_status=row["website_text_status"],
        label=f"{shard_name}:website",
        pending_forbidden=pending_forbidden,
        errors=errors,
    )
    _verify_one_text_value(
        tag_value=row["contact_website"],
        text=row["contact_website_text"],
        word_count=row["contact_website_word_count"],
        text_status=row["contact_website_text_status"],
        label=f"{shard_name}:contact_website",
        pending_forbidden=pending_forbidden,
        errors=errors,
    )


def _verify_one_text_value(
    *,
    tag_value: object,
    text: object,
    word_count: object,
    text_status: object,
    label: str,
    pending_forbidden: bool,
    errors: list[str],
) -> None:
    if text_status not in TEXT_STATUSES:
        errors.append(f"{label} has invalid text status")
        return
    if tag_value is None:
        _verify_absent_text_value(text, word_count, text_status, label, errors)
        return
    _verify_present_text_value(
        text=text,
        word_count=word_count,
        text_status=text_status,
        label=label,
        pending_forbidden=pending_forbidden,
        errors=errors,
    )


def _verify_absent_text_value(
    text: object,
    word_count: object,
    text_status: object,
    label: str,
    errors: list[str],
) -> None:
    if not _absent_text_is_consistent(text, word_count, text_status):
        errors.append(f"{label} absent tag has inconsistent text fields")


def _absent_text_is_consistent(
    text: object,
    word_count: object,
    text_status: object,
) -> bool:
    return text_status == "absent" and text is None and word_count is None


def _verify_present_text_value(
    *,
    text: object,
    word_count: object,
    text_status: object,
    label: str,
    pending_forbidden: bool,
    errors: list[str],
) -> None:
    if text_status == "absent":
        errors.append(f"{label} present tag has absent text status")
    if text_status == "pending" and pending_forbidden:
        errors.append(f"{label} remains pending after enrichment")
    _verify_terminal_text_fields(text, word_count, text_status, label, errors)


def _verify_terminal_text_fields(
    text: object,
    word_count: object,
    text_status: object,
    label: str,
    errors: list[str],
) -> None:
    if text_status == "success":
        _verify_success_text(text, word_count, label, errors)
    elif text_status == "empty":
        _verify_empty_text(text, word_count, label, errors)
    elif text is not None or word_count is not None:
        errors.append(f"{label} non-success status must have null text and word count")


def _verify_success_text(
    text: object,
    word_count: object,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(text, str):
        errors.append(f"{label} success has no text")
    elif (
        not isinstance(word_count, int)
        or isinstance(word_count, bool)
        or word_count != count_words(text)
    ):
        errors.append(f"{label} word count does not match stored text")


def _verify_empty_text(
    text: object,
    word_count: object,
    label: str,
    errors: list[str],
) -> None:
    if not _empty_text_is_consistent(text, word_count):
        errors.append(f"{label} empty result has inconsistent text fields")


def _empty_text_is_consistent(text: object, word_count: object) -> bool:
    return text == "" and word_count == 0
