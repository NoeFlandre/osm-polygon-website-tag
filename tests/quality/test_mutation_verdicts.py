from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.quality import mutation_verdicts

_MODULE = "osm_polygon_website_tag.pipeline.analyze"
_FIRST = f"{_MODULE}.first__mutmut_1"
_SECOND = f"{_MODULE}.second__mutmut_1"
_FIRST_FILTER = f"{_MODULE}.first__mutmut_*"


def _metadata(
    root: Path,
    *,
    name: str = "analyze.py",
    mutants: dict[str, int | None] | None = None,
) -> Path:
    path = root / "mutants" / "src" / "osm_polygon_website_tag" / "pipeline" / f"{name}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    exit_codes = mutants or {"mutant": 1}
    path.write_text(
        json.dumps(
            {
                "exit_code_by_key": exit_codes,
                "hash_by_function_name": {"function": "hash"},
                "type_check_error_by_key": {},
                "durations_by_key": dict.fromkeys(exit_codes, 0.1),
                "estimated_durations_by_key": dict.fromkeys(exit_codes, 0.1),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_valid_metadata_rejects_unreadable_and_malformed_documents(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.py.meta"
    assert not mutation_verdicts._valid_metadata(path)

    path.write_bytes(b"\xff")
    assert not mutation_verdicts._valid_metadata(path)

    path.write_text("{", encoding="utf-8")
    assert not mutation_verdicts._valid_metadata(path)

    path.write_text("[]", encoding="utf-8")
    assert not mutation_verdicts._valid_metadata(path)


def test_valid_metadata_rejects_unexpected_fields_and_invalid_mappings(tmp_path: Path) -> None:
    path = _metadata(tmp_path)
    valid = json.loads(path.read_text(encoding="utf-8"))
    invalid_documents = []

    missing_required = deepcopy(valid)
    missing_required.pop("exit_code_by_key")
    invalid_documents.append(missing_required)

    extra_field = deepcopy(valid)
    extra_field["future_field"] = {}
    invalid_documents.append(extra_field)

    invalid_maps = deepcopy(valid)
    invalid_maps["durations_by_key"] = []
    invalid_documents.append(invalid_maps)

    invalid_exit_code = deepcopy(valid)
    invalid_exit_code["exit_code_by_key"]["mutant"] = True
    invalid_documents.append(invalid_exit_code)

    invalid_hash = deepcopy(valid)
    invalid_hash["hash_by_function_name"]["function"] = 1
    invalid_documents.append(invalid_hash)

    invalid_type_error = deepcopy(valid)
    invalid_type_error["type_check_error_by_key"]["function"] = False
    invalid_documents.append(invalid_type_error)

    invalid_duration = deepcopy(valid)
    invalid_duration["durations_by_key"]["mutant"] = float("nan")
    invalid_documents.append(invalid_duration)

    for document in invalid_documents:
        path.write_text(json.dumps(document), encoding="utf-8")
        assert not mutation_verdicts._valid_metadata(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.1, True),
        (1, True),
        (True, False),
        (float("nan"), False),
        (float("inf"), False),
        (object(), False),
        (10**1000, False),
    ],
)
def test_duration_validation(value: object, expected: bool) -> None:
    assert mutation_verdicts._is_duration(value) is expected


def test_verify_rejects_missing_metadata(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no pipeline mutants"):
        mutation_verdicts.verify_shard(tmp_path, _MODULE, [_FIRST_FILTER])


def test_verify_rejects_a_filter_group_without_mutants(tmp_path: Path) -> None:
    _metadata(tmp_path, mutants={"other": 1})

    with pytest.raises(RuntimeError, match="no pipeline mutants"):
        mutation_verdicts.verify_shard(tmp_path, _MODULE, [_FIRST_FILTER])


def test_verify_rejects_unchecked_mutants_inside_the_group(tmp_path: Path) -> None:
    _metadata(tmp_path, mutants={_FIRST: 1, _SECOND: None})

    with pytest.raises(RuntimeError, match="not checked"):
        mutation_verdicts.verify_shard(tmp_path, _MODULE, [f"{_MODULE}.second__mutmut_*"])


def test_verify_rejects_interrupted_mutants_inside_the_group(tmp_path: Path) -> None:
    _metadata(tmp_path, mutants={_FIRST: 2})

    with pytest.raises(RuntimeError, match="interrupted"):
        mutation_verdicts.verify_shard(tmp_path, _MODULE, [_FIRST_FILTER])


def test_verify_ignores_unchecked_mutants_outside_the_group(tmp_path: Path) -> None:
    _metadata(tmp_path, mutants={_FIRST: 1, _SECOND: None})

    mutation_verdicts.verify_shard(tmp_path, _MODULE, [_FIRST_FILTER])


def test_verify_rejects_filters_outside_the_selected_module(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside"):
        mutation_verdicts.verify_shard(
            tmp_path,
            _MODULE,
            ["osm_polygon_website_tag.reporting.card.render__mutmut_*"],
        )


def test_verify_rejects_invalid_metadata_types(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path, mutants={_FIRST: 1})
    contents = json.loads(metadata.read_text(encoding="utf-8"))
    contents["exit_code_by_key"][_FIRST] = True
    metadata.write_text(json.dumps(contents), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid pipeline mutation metadata"):
        mutation_verdicts.verify_shard(tmp_path, _MODULE, [_FIRST_FILTER])


def test_verify_rejects_metadata_without_mutants(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    contents = json.loads(metadata.read_text(encoding="utf-8"))
    contents["exit_code_by_key"] = {}
    metadata.write_text(json.dumps(contents), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no pipeline mutants"):
        mutation_verdicts.verify_shard(tmp_path, _MODULE, [_FIRST_FILTER])


def test_verify_rejects_a_module_outside_pipeline(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside"):
        mutation_verdicts.verify_shard(
            tmp_path,
            "osm_polygon_website_tag.reporting.card",
            ["osm_polygon_website_tag.reporting.card.render__mutmut_*"],
        )


def test_main_dispatches_the_verify_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation_verdicts.py",
            "verify",
            "--root",
            str(tmp_path),
            "--module",
            _MODULE,
            "--filters",
            _FIRST_FILTER,
            f"{_MODULE}.second__mutmut_*",
        ],
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(mutation_verdicts, "verify_shard", lambda *args: calls.append(args))

    mutation_verdicts.main()

    assert calls == [(tmp_path, _MODULE, [_FIRST_FILTER, f"{_MODULE}.second__mutmut_*"])]


def test_main_reports_verification_errors(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation_verdicts.py",
            "verify",
            "--root",
            str(tmp_path),
            "--module",
            _MODULE,
            "--filters",
            _FIRST_FILTER,
        ],
    )

    def fail_verification(root: Path, module: str, filters: list[str]) -> None:
        raise RuntimeError("incomplete verdicts")

    monkeypatch.setattr(mutation_verdicts, "verify_shard", fail_verification)

    with pytest.raises(SystemExit, match="1"):
        mutation_verdicts.main()

    assert capsys.readouterr().err == "error: incomplete verdicts\n"
