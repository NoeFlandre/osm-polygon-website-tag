"""Tests for fail-closed path safety validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_website_tag.runtime.safety import (
    UnsafePathError,
    assert_path_safe_against,
    assert_path_safe_outside,
    normalize_path,
)


def test_normalize_path_resolves(tmp_path: Path) -> None:
    p = normalize_path(tmp_path)
    assert p == tmp_path.resolve()


def test_normalize_path_handles_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    p = normalize_path(".")
    assert p == tmp_path.resolve()


def test_assert_path_safe_outside_allows_truly_separate(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    inside = outside / "child"
    inside.mkdir()
    forbidden = tmp_path / "forbidden"
    assert_path_safe_outside(inside, forbidden)


def test_assert_path_safe_outside_rejects_path_equal_to_forbidden(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        assert_path_safe_outside(tmp_path, tmp_path)


def test_assert_path_safe_outside_rejects_path_inside_forbidden(tmp_path: Path) -> None:
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    inside = forbidden / "child"
    inside.mkdir()
    with pytest.raises(UnsafePathError):
        assert_path_safe_outside(inside, forbidden)


def test_assert_path_safe_outside_rejects_nested_inside(tmp_path: Path) -> None:
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    deeper = forbidden / "a" / "b" / "c"
    deeper.mkdir(parents=True)
    with pytest.raises(UnsafePathError):
        assert_path_safe_outside(deeper, forbidden)


def test_assert_path_safe_outside_does_not_raise_on_sibling(tmp_path: Path) -> None:
    # /tmp/abc/safe vs /tmp/abc/forbidden - safe is a sibling, not inside.
    a = tmp_path / "a"
    a.mkdir()
    safe = a / "safe"
    safe.mkdir()
    forbidden = a / "forbidden"
    forbidden.mkdir()
    assert_path_safe_outside(safe, forbidden)


def test_assert_path_safe_outside_handles_missing_forbidden(tmp_path: Path) -> None:
    """A missing forbidden path is still treated as a containment boundary."""
    target = tmp_path / "child"
    target.mkdir()
    forbidden = tmp_path / "missing"
    assert_path_safe_outside(target, forbidden)


def test_assert_path_safe_against_fails_closed_for_seagate() -> None:
    """The Seagate PBF directory must never be allowed as an output root."""
    seagate = Path("/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw")
    child = seagate / "tmp"
    with pytest.raises(UnsafePathError):
        assert_path_safe_against(child, seagate)
    with pytest.raises(UnsafePathError):
        assert_path_safe_against(seagate, seagate)


def test_assert_path_safe_against_accepts_unrelated_path(tmp_path: Path) -> None:
    safe = tmp_path / "output"
    safe.mkdir()
    seagate = Path("/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw")
    assert_path_safe_against(safe, seagate)
