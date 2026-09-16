"""Contract tests for the card/report release path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.reporting.test_finalize import _setup

from osm_polygon_website_tag.publishing.release import (
    CARD_RELEASE_FILES,
    ReleasedFile,
    build_card_release_plan,
    release_card_and_stats,
)
from osm_polygon_website_tag.reporting.finalize import finalize_run
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET


class _RecordingUploader:
    """Capture the exact upload call a release would make."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, run_dir: Path, **kwargs: Any) -> None:
        self.calls.append({"run_dir": run_dir, **kwargs})


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    root, _state = _setup(tmp_path)
    assert finalize_run(root).ok
    return root


def test_dry_run_plans_exactly_the_card_and_report(run_dir: Path) -> None:
    uploader = _RecordingUploader()

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET, uploader=uploader)

    assert report.repo_id == DEFAULT_HF_DATASET
    assert report.published is False
    assert report.revision is None
    assert [item.relative_path for item in report.files] == list(CARD_RELEASE_FILES)
    assert report.verified_shards
    assert uploader.calls == []


def test_apply_uploads_only_the_card_and_report_then_verifies(run_dir: Path) -> None:
    uploader = _RecordingUploader()
    seen: list[tuple[str, tuple[ReleasedFile, ...]]] = []

    def verifier(repo_id: str, files: tuple[ReleasedFile, ...]) -> str:
        seen.append((repo_id, files))
        return "cafebabe"

    report = release_card_and_stats(
        run_dir,
        confirm_repo=DEFAULT_HF_DATASET,
        apply=True,
        uploader=uploader,
        verifier=verifier,
    )

    assert report.published is True
    assert report.revision == "cafebabe"
    assert len(uploader.calls) == 1
    call = uploader.calls[0]
    assert call["repo_id"] == DEFAULT_HF_DATASET
    assert [item.relative_path for item in call["files"]] == list(CARD_RELEASE_FILES)
    assert seen == [(DEFAULT_HF_DATASET, report.files)]


def test_second_release_is_a_byte_stable_no_op(run_dir: Path) -> None:
    first = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)
    stats = run_dir / "stats.json"
    before = (stats.read_bytes(), stats.stat().st_mtime_ns)

    second = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert second.files == first.files
    assert second.recomputed is False
    assert (stats.read_bytes(), stats.stat().st_mtime_ns) == before


def test_wrong_repository_confirmation_is_refused(run_dir: Path) -> None:
    with pytest.raises(ValueError, match="repository confirmation"):
        release_card_and_stats(run_dir, confirm_repo="someone-else/osm-polygon-website-tag")


def test_unverified_run_is_refused(run_dir: Path) -> None:
    (run_dir / "polygons" / "monaco-latest.parquet").unlink()

    with pytest.raises(ValueError, match="refusing to release"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_missing_report_is_refused(run_dir: Path) -> None:
    (run_dir / "stats.json").unlink()

    with pytest.raises(ValueError, match="missing card release artifact"):
        build_card_release_plan(run_dir)


def test_empty_remote_revision_is_refused(run_dir: Path) -> None:
    with pytest.raises(ValueError, match="empty revision"):
        release_card_and_stats(
            run_dir,
            confirm_repo=DEFAULT_HF_DATASET,
            apply=True,
            uploader=_RecordingUploader(),
            verifier=lambda repo_id, files: "",
        )
