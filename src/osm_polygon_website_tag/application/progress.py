"""Terminal-aware progress reporting for application commands."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO

from tqdm import tqdm


def report_progress(callback: Callable[[str], None] | None, message: str) -> None:
    """Send ``message`` to ``callback`` when one is configured."""
    if callback is not None:
        callback(message)


class CountedProgress(str):
    """A ``[current/total] text`` progress message that also carries its counts.

    The string value is the legacy prefixed text, so logs, list comparisons and
    plain-string callbacks behave as they did with a formatted string. The
    reporter reads the fields directly instead of parsing the prefix.

    Contract: multi-line text is not counted, as the legacy string is not
    (the reporter prints it as an ordinary line). Empty text is counted and
    gives the bar an empty description. No producer sends empty text. A source
    file name can contain a newline, so multi-line text can occur.
    """

    current: int
    total: int
    text: str

    def __new__(cls, current: int, total: int, text: str) -> CountedProgress:
        if not isinstance(text, str):
            raise TypeError(f"counted progress text must be a str, not {type(text).__name__}")
        event = super().__new__(cls, f"[{current}/{total}] {text}")
        event.current = current
        event.total = total
        event.text = text
        return event

    def __reduce__(self) -> tuple[type[CountedProgress], tuple[int, int, str]]:
        """Let ``copy`` and ``pickle`` rebuild the message from its fields."""
        return (CountedProgress, (self.current, self.total, self.text))


def counted_progress(current: int, total: int, text: str) -> CountedProgress:
    """Return a counted progress message for item ``current`` of ``total``."""
    return CountedProgress(current, total, text)


def _require_str(message: object) -> None:
    """Refuse non-text messages in both modes, as before the counted-progress change."""
    if not isinstance(message, str):
        raise TypeError(f"progress message must be a str, not {type(message).__name__}")


def _counted_fields(message: str) -> tuple[int, int, str] | None:
    """Return (current, total, text) for a typed counted message, or None for any other.

    Plain strings are never counted, so a reworded producer cannot lose its bar
    silently by changing a prefix. Multi-line text stays ordinary, as the
    counted string does, so both forms print the same line.
    """
    if isinstance(message, CountedProgress) and "\n" not in message.text:
        return message.current, message.total, message.text
    return None


class ProgressReporter:
    """Render workflow messages as stable logs or an interactive tqdm bar."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        interactive: bool | None = None,
        quiet: bool = False,
    ) -> None:
        self._quiet = quiet
        self._stream = stream or sys.stderr
        self._interactive = self._stream.isatty() if interactive is None else interactive
        self._bar: tqdm[object] | None = None
        self._last_current: int | None = None

    def __call__(self, message: str) -> None:
        if self._quiet:
            return
        _require_str(message)
        if not self._interactive:
            self._write_plain(message)
            return
        fields = _counted_fields(message)
        if fields is None:
            self._write_uncounted(message)
            return
        self._update_counted(*fields)

    def _write_plain(self, message: str) -> None:
        """Write a stable line when no terminal progress bar is active."""
        print(message, file=self._stream, flush=True)

    def _write_uncounted(self, message: str) -> None:
        """Finish a bar before emitting an ordinary interactive message."""
        self._finish_bar(completed=True)
        tqdm.write(message, file=self._stream)

    def _update_counted(self, current_value: int, total_value: int, description: str) -> None:
        """Update the bounded tqdm display for one counted workflow message."""
        if self._last_current is not None and current_value < self._last_current:
            self._finish_bar(completed=True)
        if self._bar is None:
            self._bar = tqdm(
                total=total_value,
                file=self._stream,
                unit="pbf",
                dynamic_ncols=True,
            )
        self._last_current = current_value
        self._bar.set_description_str(description)
        completed_before_current = current_value - 1
        if completed_before_current > self._bar.n:
            self._bar.update(completed_before_current - self._bar.n)
        self._bar.refresh()

    def close(self, *, completed: bool) -> None:
        """Close any active bar, marking it complete only on success."""
        self._finish_bar(completed=completed)

    def _finish_bar(self, *, completed: bool) -> None:
        if self._bar is None:
            return
        if completed and self._bar.total is not None and self._bar.n < self._bar.total:
            self._bar.update(self._bar.total - self._bar.n)
        self._bar.close()
        self._bar = None
        self._last_current = None


__all__ = ["CountedProgress", "ProgressReporter", "counted_progress"]
