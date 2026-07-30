"""Fail-closed path safety validation.

Any output, cache, staging, or temporary path must be *outside* and
never equal to the immutable source directory. The validate functions
raise :class:`UnsafePathError` rather than returning a status so that
unsafe paths cannot be silently ignored.

The validation is fail-closed: missing source directories are still treated
as containment boundaries, because a missing source today does not
guarantee a missing source tomorrow.
"""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a path is equal to or contained by a forbidden directory."""


def normalize_path(path: str | Path) -> Path:
    """Return a fully-resolved absolute :class:`Path` for ``path``."""
    return Path(path).expanduser().resolve(strict=False)


def _is_equal_or_inside(candidate: Path, boundary: Path) -> bool:
    """Return ``True`` iff ``candidate`` equals ``boundary`` or is nested under it."""
    candidate = normalize_path(candidate)
    boundary = normalize_path(boundary)
    if candidate == boundary:
        return True
    try:
        candidate.relative_to(boundary)
        return True
    except ValueError:
        return False


def assert_path_safe_against(candidate: str | Path, forbidden: str | Path) -> Path:
    """Resolve ``candidate`` and raise :class:`UnsafePathError` if it equals
    or is contained by ``forbidden``.

    Returns the resolved candidate path for convenience.
    """
    candidate_resolved = normalize_path(candidate)
    forbidden_resolved = normalize_path(forbidden)
    if _is_equal_or_inside(candidate_resolved, forbidden_resolved):
        raise UnsafePathError(
            f"Refusing path {candidate_resolved}: it is the same as or contained by "
            f"the forbidden root {forbidden_resolved}."
        )
    return candidate_resolved


def assert_path_safe_outside(candidate: str | Path, forbidden: str | Path) -> Path:
    """Synonym for :func:`assert_path_safe_against` kept for readability."""
    return assert_path_safe_against(candidate, forbidden)
