"""Tests for resuming only an interrupted, same-commit mutation sweep."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.quality import mutation_checkpoint


def _metadata(
    root: Path,
    *,
    name: str = "pipeline.py",
    exit_code: int | None = 1,
    cached: bool = False,
) -> Path:
    base = root / ".mutation-checkpoint" if cached else root
    path = base / "mutants" / "src" / "osm_polygon_website_tag" / "pipeline" / f"{name}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {"mutant": exit_code},
                "hash_by_function_name": {"function": "hash"},
                "type_check_error_by_key": {},
                "durations_by_key": {"mutant": 0.1},
                "estimated_durations_by_key": {"mutant": 0.1},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_valid_metadata_rejects_unreadable_and_malformed_documents(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.py.meta"
    assert not mutation_checkpoint._valid_metadata(path)

    path.write_bytes(b"\xff")
    assert not mutation_checkpoint._valid_metadata(path)

    path.write_text("{", encoding="utf-8")
    assert not mutation_checkpoint._valid_metadata(path)

    path.write_text("[]", encoding="utf-8")
    assert not mutation_checkpoint._valid_metadata(path)


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
        assert not mutation_checkpoint._valid_metadata(path)


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
    assert mutation_checkpoint._is_duration(value) is expected


def test_prepare_preserves_valid_metadata_only_after_an_interrupted_sweep(tmp_path: Path) -> None:
    cached_metadata = _metadata(tmp_path, cached=True)
    (tmp_path / ".mutation-checkpoint" / "status").write_text("partial\n", encoding="utf-8")

    mutation_checkpoint.prepare_checkpoint(tmp_path)

    assert (tmp_path / "mutants/src/osm_polygon_website_tag/pipeline/pipeline.py.meta").is_file()
    assert cached_metadata.exists() is False
    assert not (tmp_path / ".mutation-checkpoint").exists()


def test_prepare_discards_results_from_a_completed_sweep(tmp_path: Path) -> None:
    cached_metadata = _metadata(tmp_path, cached=True)
    stale_metadata = _metadata(tmp_path, exit_code=0)
    (tmp_path / ".mutation-checkpoint" / "status").write_text("complete\n", encoding="utf-8")

    mutation_checkpoint.prepare_checkpoint(tmp_path)

    assert not stale_metadata.exists()
    assert cached_metadata.exists() is False


def test_prepare_discards_malformed_partial_metadata(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path, cached=True)
    stale_metadata = _metadata(tmp_path, exit_code=0)
    metadata.write_text('{"exit_code_by_key":', encoding="utf-8")
    (tmp_path / ".mutation-checkpoint" / "status").write_text("partial\n", encoding="utf-8")

    mutation_checkpoint.prepare_checkpoint(tmp_path)

    assert not metadata.exists()
    assert not stale_metadata.exists()


def test_prepare_discards_results_when_the_checkpoint_status_is_missing(tmp_path: Path) -> None:
    cached_metadata = _metadata(tmp_path, cached=True)
    stale_metadata = _metadata(tmp_path, exit_code=0)

    mutation_checkpoint.prepare_checkpoint(tmp_path)

    assert not stale_metadata.exists()
    assert not (tmp_path / "mutants/src/osm_polygon_website_tag/pipeline/pipeline.py.meta").exists()
    assert not cached_metadata.exists()
    assert not (tmp_path / ".mutation-checkpoint").exists()


def test_mark_records_success_only_for_a_successful_sweep(tmp_path: Path) -> None:
    _metadata(tmp_path)
    cache_dir = tmp_path / ".mutation-checkpoint"
    marker = cache_dir / "status"

    mutation_checkpoint.mark_checkpoint("success", tmp_path)
    assert marker.read_text(encoding="utf-8") == "complete\n"
    assert not (cache_dir / "mutants").exists()

    mutation_checkpoint.mark_checkpoint("cancelled", tmp_path)
    assert marker.read_text(encoding="utf-8") == "partial\n"
    assert (cache_dir / "mutants/src/osm_polygon_website_tag/pipeline/pipeline.py.meta").is_file()


def test_mark_does_not_cache_invalid_metadata(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    metadata.write_text('{"exit_code_by_key": []}', encoding="utf-8")

    mutation_checkpoint.mark_checkpoint("failed", tmp_path)

    cache_dir = tmp_path / ".mutation-checkpoint"
    assert (cache_dir / "status").read_text(encoding="utf-8") == "partial\n"
    assert not (cache_dir / "mutants").exists()


def test_verify_rejects_a_scope_without_mutant_metadata(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(RuntimeError, match="no pipeline mutants"):
        mutation_checkpoint.verify_checkpoint(tmp_path)


def test_verify_rejects_unchecked_mutants(tmp_path: Path) -> None:
    import pytest

    _metadata(tmp_path, exit_code=None)

    with pytest.raises(RuntimeError, match="not checked"):
        mutation_checkpoint.verify_checkpoint(tmp_path)


def test_verify_rejects_invalid_metadata_types(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    contents = json.loads(metadata.read_text(encoding="utf-8"))
    contents["exit_code_by_key"]["mutant"] = True
    metadata.write_text(json.dumps(contents), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid pipeline mutation metadata"):
        mutation_checkpoint.verify_checkpoint(tmp_path)


def test_verify_rejects_metadata_without_mutants(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    contents = json.loads(metadata.read_text(encoding="utf-8"))
    contents["exit_code_by_key"] = {}
    metadata.write_text(json.dumps(contents), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no pipeline mutants"):
        mutation_checkpoint.verify_checkpoint(tmp_path)


def test_verify_accepts_a_complete_pipeline_sweep(tmp_path: Path) -> None:
    _metadata(tmp_path)

    mutation_checkpoint.verify_checkpoint(tmp_path)


@pytest.mark.parametrize(
    ("command", "function_name", "extra_arguments"),
    [
        ("prepare", "prepare_checkpoint", []),
        ("mark", "mark_checkpoint", ["--outcome", "success"]),
        ("verify", "verify_checkpoint", []),
    ],
)
def test_main_dispatches_commands(
    monkeypatch,
    tmp_path: Path,
    command: str,
    function_name: str,
    extra_arguments: list[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["mutation_checkpoint.py", command, *extra_arguments, "--root", str(tmp_path)],
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        mutation_checkpoint,
        function_name,
        lambda *args: calls.append(args),
    )

    mutation_checkpoint.main()

    if command == "mark":
        assert calls == [("success", tmp_path)]
    else:
        assert calls == [(tmp_path,)]


def test_main_reports_verification_errors(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["mutation_checkpoint.py", "verify", "--root", str(tmp_path)])

    def fail_verification(root: Path) -> None:
        raise RuntimeError("incomplete verdicts")

    monkeypatch.setattr(mutation_checkpoint, "verify_checkpoint", fail_verification)

    with pytest.raises(SystemExit, match="1"):
        mutation_checkpoint.main()

    assert capsys.readouterr().err == "error: incomplete verdicts\n"
