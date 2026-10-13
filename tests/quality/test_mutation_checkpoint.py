"""Tests for resuming only an interrupted, same-commit mutation sweep."""

from __future__ import annotations

import json
from pathlib import Path

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
