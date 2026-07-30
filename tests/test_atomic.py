"""Tests for atomic file writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_website_tag.atomic import atomic_promote_bundle, atomic_write_file


def test_atomic_write_replaces_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    src = tmp_path / "out.txt.tmp"
    src.write_text("new", encoding="utf-8")
    atomic_write_file(src, target)
    assert target.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_atomic_write_creates_new_file(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("hello", encoding="utf-8")
    target = tmp_path / "target.txt"
    atomic_write_file(src, target)
    assert target.read_text(encoding="utf-8") == "hello"
    assert not src.exists()


def test_atomic_write_never_leaves_temp(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("hello", encoding="utf-8")
    target = tmp_path / "target.txt"
    atomic_write_file(src, target)
    assert not src.exists()
    assert target.exists()


def test_atomic_write_partial_failure_does_not_overwrite(tmp_path: Path) -> None:
    """If the source file disappears mid-write, the target must be untouched."""
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    src = tmp_path / "src.txt"
    src.write_text("replacement", encoding="utf-8")

    def _move(_src: Path, _dst: Path) -> None:
        # Simulate a partial failure: remove the source but raise before rename.
        _src.unlink()
        raise OSError("simulated")

    with pytest.raises(OSError):
        atomic_write_file(src, target, mover=_move)
    assert target.read_text(encoding="utf-8") == "original"


def test_atomic_promote_bundle_rolls_back_all_targets_on_failure(tmp_path: Path) -> None:
    targets = [tmp_path / f"target-{index}.txt" for index in range(3)]
    staged = [tmp_path / f"staged-{index}.txt" for index in range(3)]
    for index, path in enumerate(targets):
        path.write_text(f"old-{index}")
    for index, path in enumerate(staged):
        path.write_text(f"new-{index}")
    calls = 0

    def failing_mover(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected promotion failure")
        source.replace(target)

    with pytest.raises(OSError, match="injected"):
        atomic_promote_bundle(
            list(zip(staged, targets, strict=True)),
            mover=failing_mover,
        )

    assert [path.read_text() for path in targets] == ["old-0", "old-1", "old-2"]
