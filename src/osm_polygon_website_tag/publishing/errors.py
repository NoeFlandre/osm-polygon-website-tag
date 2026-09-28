"""Exceptions raised by the publishing package."""

from __future__ import annotations


class TrackioUnavailableError(RuntimeError):
    """The optional ``trackio`` package is not installed."""


__all__ = ["TrackioUnavailableError"]
