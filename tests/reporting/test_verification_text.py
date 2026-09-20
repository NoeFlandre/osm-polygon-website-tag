"""Focused tests for the text invariant verification helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_website_tag.reporting.verification import text


def test_verify_text_invariants_selects_and_sorts_public_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    (polygons / "b.parquet").touch()
    (polygons / "a.parquet").touch()
    observed: dict[str, object] = {}

    def verify_paths(paths: list[Path], status: object, errors: list[str]) -> None:
        observed.update(paths=paths, status=status, errors=errors)

    monkeypatch.setattr(text, "verify_text_paths", verify_paths)
    errors: list[str] = []

    text.verify_text_invariants(tmp_path, "verified", errors)

    assert observed == {
        "paths": [polygons / "a.parquet", polygons / "b.parquet"],
        "status": "verified",
        "errors": errors,
    }


@pytest.mark.parametrize(
    "status,pending_forbidden",
    [
        ("enriched", True),
        ("analyzed", True),
        ("card_built", True),
        ("verified", True),
        ("complete", True),
        ("extracting", False),
        (None, False),
    ],
)
def test_verify_text_paths_sorts_shards_and_derives_pending_policy(
    status: object,
    pending_forbidden: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [Path("b.parquet"), Path("a.parquet")]
    observed: list[tuple[Path, bool, list[str]]] = []
    errors: list[str] = []

    def verify_shard(
        shard: Path,
        actual_pending_forbidden: bool,
        actual_errors: list[str],
    ) -> None:
        observed.append((shard, actual_pending_forbidden, actual_errors))

    monkeypatch.setattr(text, "_verify_text_shard", verify_shard)

    text.verify_text_paths(paths, status, errors)

    assert observed == [
        (Path("a.parquet"), pending_forbidden, errors),
        (Path("b.parquet"), pending_forbidden, errors),
    ]


def test_verify_text_shard_reads_batches_and_forwards_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_row: dict[str, object] = {"website": "first"}
    second_row: dict[str, object] = {"website": "second"}
    observed: dict[str, object] = {}

    class FakeBatch:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def to_pylist(self) -> list[dict[str, object]]:
            return self.rows

    class FakeParquet:
        def iter_batches(
            self,
            *,
            columns: list[str],
            batch_size: int,
        ) -> list[FakeBatch]:
            observed["columns"] = columns
            observed["batch_size"] = batch_size
            return [FakeBatch([first_row]), FakeBatch([second_row])]

    shard = Path("shard.parquet")
    errors: list[str] = []

    def parquet_file(path: Path) -> FakeParquet:
        observed["path"] = path
        return FakeParquet()

    row_calls: list[tuple[dict[str, object], str, bool, list[str]]] = []

    def verify_row(
        row: dict[str, object],
        shard_name: str,
        pending_forbidden: bool,
        actual_errors: list[str],
    ) -> None:
        row_calls.append((row, shard_name, pending_forbidden, actual_errors))

    monkeypatch.setattr(text.pq, "ParquetFile", parquet_file)
    monkeypatch.setattr(text, "_verify_text_row", verify_row)

    text._verify_text_shard(shard, True, errors)

    assert observed == {
        "path": shard,
        "columns": [
            "website",
            "contact_website",
            "website_text",
            "website_word_count",
            "website_text_status",
            "contact_website_text",
            "contact_website_word_count",
            "contact_website_text_status",
        ],
        "batch_size": 512,
    }
    assert row_calls == [
        (first_row, "shard.parquet", True, errors),
        (second_row, "shard.parquet", True, errors),
    ]


def test_verify_text_shard_reports_reader_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def parquet_file(_path: Path) -> object:
        raise RuntimeError("broken parquet")

    monkeypatch.setattr(text.pq, "ParquetFile", parquet_file)
    errors: list[str] = []

    text._verify_text_shard(Path("broken.parquet"), False, errors)

    assert errors == ["text invariant verification failed for broken.parquet: broken parquet"]


def test_verify_text_row_forwards_website_and_contact_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row: dict[str, object] = {
        "website": "https://example.org",
        "website_text": "website text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website": "https://contact.example.org",
        "contact_website_text": "contact text",
        "contact_website_word_count": 2,
        "contact_website_text_status": "success",
    }
    calls: list[dict[str, object]] = []

    def verify_value(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(text, "_verify_one_text_value", verify_value)
    errors: list[str] = []

    text._verify_text_row(row, "a.parquet", True, errors)

    assert calls == [
        {
            "tag_value": "https://example.org",
            "text": "website text",
            "word_count": 2,
            "text_status": "success",
            "label": "a.parquet:website",
            "pending_forbidden": True,
            "errors": errors,
        },
        {
            "tag_value": "https://contact.example.org",
            "text": "contact text",
            "word_count": 2,
            "text_status": "success",
            "label": "a.parquet:contact_website",
            "pending_forbidden": True,
            "errors": errors,
        },
    ]


def test_verify_one_text_value_rejects_invalid_status_without_delegating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        text,
        "_verify_absent_text_value",
        lambda *_args, **_kwargs: calls.append("absent"),
    )
    monkeypatch.setattr(
        text,
        "_verify_present_text_value",
        lambda **_kwargs: calls.append("present"),
    )
    errors: list[str] = []

    text._verify_one_text_value(
        tag_value="https://example.org",
        text="text",
        word_count=1,
        text_status="invalid",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )

    assert errors == ["website has invalid text status"]
    assert calls == []


@pytest.mark.parametrize("tag_value,delegate", [(None, "absent"), ("tag", "present")])
def test_verify_one_text_value_delegates_by_tag_presence(
    tag_value: object,
    delegate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def absent(*args: object, **kwargs: object) -> None:
        calls.append(("absent", {"args": args, **kwargs}))

    def present(**kwargs: object) -> None:
        calls.append(("present", kwargs))

    monkeypatch.setattr(text, "_verify_absent_text_value", absent)
    monkeypatch.setattr(text, "_verify_present_text_value", present)
    errors: list[str] = []

    text._verify_one_text_value(
        tag_value=tag_value,
        text="value",
        word_count=1,
        text_status="success",
        label="website",
        pending_forbidden=True,
        errors=errors,
    )

    assert errors == []
    assert [name for name, _payload in calls] == [delegate]
    payload = calls[0][1]
    if delegate == "absent":
        assert payload == {
            "args": ("value", 1, "success", "website", errors),
        }
    else:
        assert payload == {
            "text": "value",
            "word_count": 1,
            "text_status": "success",
            "label": "website",
            "pending_forbidden": True,
            "errors": errors,
        }


@pytest.mark.parametrize(
    "text_value,word_count,text_status,expected",
    [
        (None, None, "absent", []),
        ("text", None, "absent", ["website absent tag has inconsistent text fields"]),
        (None, 1, "absent", ["website absent tag has inconsistent text fields"]),
    ],
)
def test_verify_absent_text_value_reports_inconsistent_fields(
    text_value: object,
    word_count: object,
    text_status: object,
    expected: list[str],
) -> None:
    errors: list[str] = []

    text._verify_absent_text_value(
        text_value,
        word_count,
        text_status,
        "website",
        errors,
    )

    assert errors == expected


@pytest.mark.parametrize(
    "text_status,pending_forbidden,expected",
    [
        ("absent", False, ["website present tag has absent text status"]),
        ("pending", True, ["website remains pending after enrichment"]),
        ("pending", False, []),
        ("success", False, []),
    ],
)
def test_verify_present_text_value_checks_status_policy_and_delegates(
    text_status: object,
    pending_forbidden: bool,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_calls: list[tuple[object, object, object, str, list[str]]] = []

    def verify_terminal(
        value: object,
        words: object,
        status: object,
        label: str,
        errors: list[str],
    ) -> None:
        terminal_calls.append((value, words, status, label, errors))

    monkeypatch.setattr(text, "_verify_terminal_text_fields", verify_terminal)
    errors: list[str] = []

    text._verify_present_text_value(
        text="value",
        word_count=1,
        text_status=text_status,
        label="website",
        pending_forbidden=pending_forbidden,
        errors=errors,
    )

    assert errors == expected
    assert terminal_calls == [("value", 1, text_status, "website", errors)]


def test_verify_terminal_text_fields_dispatches_and_rejects_nonterminal_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success_calls: list[tuple[object, object, str, list[str]]] = []
    empty_calls: list[tuple[object, object, str, list[str]]] = []

    monkeypatch.setattr(
        text,
        "_verify_success_text",
        lambda value, words, label, errors: success_calls.append((value, words, label, errors)),
    )
    monkeypatch.setattr(
        text,
        "_verify_empty_text",
        lambda value, words, label, errors: empty_calls.append((value, words, label, errors)),
    )

    success_errors: list[str] = []
    text._verify_terminal_text_fields("text", 1, "success", "website", success_errors)
    empty_errors: list[str] = []
    text._verify_terminal_text_fields("", 0, "empty", "website", empty_errors)
    pending_errors: list[str] = []
    text._verify_terminal_text_fields(None, None, "pending", "website", pending_errors)
    invalid_errors: list[str] = []
    text._verify_terminal_text_fields(None, 1, "pending", "website", invalid_errors)
    text._verify_terminal_text_fields("text", None, "pending", "website", invalid_errors)

    assert success_calls == [("text", 1, "website", success_errors)]
    assert empty_calls == [("", 0, "website", empty_errors)]
    assert pending_errors == []
    assert invalid_errors == [
        "website non-success status must have null text and word count",
        "website non-success status must have null text and word count",
    ]


@pytest.mark.parametrize(
    "text_value,word_count,expected",
    [
        (None, 1, ["website success has no text"]),
        ("one two", 1, ["website word count does not match stored text"]),
        ("one", True, ["website word count does not match stored text"]),
        ("one two", True, ["website word count does not match stored text"]),
        ("one two", 2, []),
    ],
)
def test_verify_success_text_requires_string_and_exact_word_count(
    text_value: object,
    word_count: object,
    expected: list[str],
) -> None:
    errors: list[str] = []

    text._verify_success_text(text_value, word_count, "website", errors)

    assert errors == expected


@pytest.mark.parametrize(
    "text_value,word_count,expected",
    [
        ("", 0, []),
        (None, 0, ["website empty result has inconsistent text fields"]),
        ("", None, ["website empty result has inconsistent text fields"]),
    ],
)
def test_verify_empty_text_requires_empty_string_and_zero_words(
    text_value: object,
    word_count: object,
    expected: list[str],
) -> None:
    errors: list[str] = []

    text._verify_empty_text(text_value, word_count, "website", errors)

    assert errors == expected
