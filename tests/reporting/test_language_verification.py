"""Tests for v1.4 language-field invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.reporting.verification import language as language_module
from osm_polygon_website_tag.reporting.verification.language import verify_language_invariants


def _write_language_row(tmp_path: Path, *, probability: object) -> Path:
    path = tmp_path / "polygons" / "source.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "website": "https://example.org",
                    "website_text": "English text",
                    "website_text_status": "success",
                    "website_language": "eng_Latn",
                    "website_language_probability": probability,
                    "contact_website": None,
                    "contact_website_text": None,
                    "contact_website_text_status": "absent",
                    "contact_website_language": None,
                    "contact_website_language_probability": None,
                }
            ],
            schema=pa.schema(
                [
                    pa.field("website", pa.string()),
                    pa.field("website_text", pa.string()),
                    pa.field("website_text_status", pa.string()),
                    pa.field("website_language", pa.string()),
                    pa.field("website_language_probability", pa.float64()),
                    pa.field("contact_website", pa.string()),
                    pa.field("contact_website_text", pa.string()),
                    pa.field("contact_website_text_status", pa.string()),
                    pa.field("contact_website_language", pa.string()),
                    pa.field("contact_website_language_probability", pa.float64()),
                ]
            ),
        ),
        path,
    )
    return path


def test_verify_rejects_success_without_language_probability(tmp_path: Path) -> None:
    _write_language_row(tmp_path, probability=None)
    errors: list[str] = []

    verify_language_invariants(tmp_path, errors)

    assert any("language probability" in error for error in errors)


def test_verify_accepts_a_complete_language_pair(tmp_path: Path) -> None:
    _write_language_row(tmp_path, probability=0.91)
    errors: list[str] = []

    verify_language_invariants(tmp_path, errors)

    assert errors == []


def test_verify_language_file_reports_unreadable_and_ignores_legacy_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable_errors: list[str] = []
    monkeypatch.setattr(
        language_module.pq,
        "read_schema",
        lambda _path: (_ for _ in ()).throw(OSError("broken")),
    )

    language_module._verify_language_file(tmp_path / "broken.parquet", unreadable_errors)

    assert unreadable_errors == [f"unreadable language shard {tmp_path / 'broken.parquet'}: broken"]

    legacy_errors: list[str] = []
    monkeypatch.setattr(language_module.pq, "read_schema", lambda _path: pa.schema([]))
    monkeypatch.setattr(
        language_module,
        "_verify_language_shard",
        lambda *_args: pytest.fail("legacy shard must not be language-validated"),
    )

    language_module._verify_language_file(tmp_path / "legacy.parquet", legacy_errors)

    assert legacy_errors == []


def test_verify_language_file_forwards_the_exact_language_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.parquet"
    schema = pa.schema(
        [pa.field(name, pa.string()) for name in language_module.LANGUAGE_COLUMN_NAMES]
    )
    calls: list[tuple[Path, list[str]]] = []
    monkeypatch.setattr(language_module.pq, "read_schema", lambda path_value: schema)
    monkeypatch.setattr(
        language_module,
        "_verify_language_shard",
        lambda path_value, errors: calls.append((path_value, errors)),
    )
    errors: list[str] = []

    language_module._verify_language_file(path, errors)

    assert calls == [(path, errors)]


def test_verify_language_invariants_scans_the_public_polygon_directory_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = Path("a.parquet")
    second = Path("b.parquet")
    directory_names: list[str] = []
    file_calls: list[tuple[Path, list[str]]] = []

    class Directory:
        def glob(self, pattern: str) -> list[Path]:
            assert pattern == "*.parquet"
            return [second, first]

    class Root:
        def __truediv__(self, name: str) -> Directory:
            directory_names.append(name)
            return Directory()

    monkeypatch.setattr(
        language_module,
        "_verify_language_file",
        lambda path, errors: file_calls.append((path, errors)),
    )
    errors: list[str] = []

    language_module.verify_language_invariants(cast(Any, Root()), errors)

    assert directory_names == ["polygons"]
    assert file_calls == [(first, errors), (second, errors)]


def test_verify_language_shard_uses_bounded_language_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.parquet"
    errors: list[str] = []
    batch = pa.RecordBatch.from_pylist(
        [
            {
                "website_text_status": "success",
                "website_language": "eng_Latn",
                "website_language_probability": 0.9,
                "contact_website_text_status": "absent",
                "contact_website_language": None,
                "contact_website_language_probability": None,
            }
        ]
    )

    class Parquet:
        schema_arrow = batch.schema

        def iter_batches(self, **kwargs: object):  # type: ignore[no-untyped-def]
            assert kwargs == {
                "batch_size": 512,
                "columns": language_module._LANGUAGE_COLUMNS,
            }
            yield batch

    batches: list[tuple[Path, int, pa.RecordBatch, list[str]]] = []
    monkeypatch.setattr(language_module.pq, "ParquetFile", lambda path_value: Parquet())
    monkeypatch.setattr(
        language_module,
        "_verify_language_batch",
        lambda path_value, number, batch_value, errors_value: batches.append(
            (path_value, number, batch_value, errors_value)
        ),
    )

    language_module._verify_language_shard(path, errors)

    assert batches == [(path, 0, batch, errors)]


def test_verify_language_batch_preserves_absolute_row_numbers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = pa.RecordBatch.from_pylist(
        [
            {
                "website_text_status": "success",
                "website_language": "eng_Latn",
                "website_language_probability": 0.9,
                "contact_website_text_status": "absent",
                "contact_website_language": None,
                "contact_website_language_probability": None,
            },
            {
                "website_text_status": "success",
                "website_language": "fra_Latn",
                "website_language_probability": 0.8,
                "contact_website_text_status": "success",
                "contact_website_language": "fra_Latn",
                "contact_website_language_probability": 0.7,
            },
        ]
    )
    errors: list[str] = []
    row_calls: list[tuple[Path, int, list[object], list[str]]] = []
    monkeypatch.setattr(
        language_module,
        "_verify_language_row",
        lambda path, row_number, values, errors_value: row_calls.append(
            (path, row_number, values, errors_value)
        ),
    )

    language_module._verify_language_batch(tmp_path / "source.parquet", 2, batch, errors)

    assert row_calls == [
        (
            tmp_path / "source.parquet",
            2 * language_module._LANGUAGE_BATCH_ROWS,
            ["success", "eng_Latn", 0.9, "absent", None, None],
            errors,
        ),
        (
            tmp_path / "source.parquet",
            2 * language_module._LANGUAGE_BATCH_ROWS + 1,
            ["success", "fra_Latn", 0.8, "success", "fra_Latn", 0.7],
            errors,
        ),
    ]


@pytest.mark.parametrize(
    ("status", "label", "probability", "expected"),
    [
        ("success", "eng_Latn", 0.9, []),
        ("success", "", 0.9, ["language label is missing"]),
        ("success", "eng_Latn", None, ["language probability is invalid"]),
        ("absent", None, None, []),
        (
            "absent",
            "eng_Latn",
            None,
            ["language fields must be null when text is not successful"],
        ),
        (
            "absent",
            None,
            0.9,
            ["language fields must be null when text is not successful"],
        ),
    ],
)
def test_verify_language_pair_enforces_nullable_success_contract(
    tmp_path: Path,
    status: str,
    label: object,
    probability: object,
    expected: list[str],
) -> None:
    errors: list[str] = []

    language_module._verify_language_pair(
        tmp_path / "source.parquet",
        3,
        "website",
        status,
        label,
        probability,
        errors,
    )

    path = tmp_path / "source.parquet"
    assert errors == [f"{path} row 3 website {fragment}" for fragment in expected]


def test_verify_language_row_forwards_both_language_pairs_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.parquet"
    errors: list[str] = []
    pair_calls: list[tuple[object, ...]] = []

    def verify_pair(*args: object) -> None:
        pair_calls.append(args)

    monkeypatch.setattr(language_module, "_verify_language_pair", verify_pair)
    values: list[object] = ["success", "eng_Latn", 0.9, "success", "fra_Latn", 0.8]

    language_module._verify_language_row(path, 7, values, errors)

    assert pair_calls == [
        (path, 7, "website", "success", "eng_Latn", 0.9, errors),
        (path, 7, "contact_website", "success", "fra_Latn", 0.8, errors),
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, True),
        (0.5, True),
        (1, True),
        (-0.1, False),
        (1.1, False),
        (True, False),
        (float("inf"), False),
        (float("nan"), False),
        ("0.5", False),
    ],
)
def test_valid_probability_accepts_only_finite_numbers_in_range(
    value: object,
    expected: bool,
) -> None:
    assert language_module._valid_probability(value) is expected


@pytest.mark.parametrize(
    ("label", "probability", "expected"),
    [(None, None, False), ("eng_Latn", None, True), (None, 0.9, True)],
)
def test_has_language_values_checks_both_nullable_fields(
    label: object,
    probability: object,
    expected: bool,
) -> None:
    assert language_module._has_language_values(label, probability) is expected
