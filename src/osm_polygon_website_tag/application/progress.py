"""Terminal-aware progress reporting for application commands."""

from __future__ import annotations

import re
import sys
from typing import TextIO

from tqdm import tqdm

_COUNTED_MESSAGE = re.compile(r"^\[(\d+)/(\d+)\] (.+)$")


class ProgressReporter:
    """Render workflow messages as stable logs or an interactive tqdm bar."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        interactive: bool | None = None,
    ) -> None:
        self._stream = stream or sys.stderr
        self._interactive = self._stream.isatty() if interactive is None else interactive
        self._bar: tqdm[object] | None = None
        self._last_current: int | None = None

    def __call__(self, message: str) -> None:
        match = _COUNTED_MESSAGE.fullmatch(message)
        if not self._interactive:
            print(message, file=self._stream, flush=True)
            return
        if match is None:
            self._finish_bar(completed=True)
            tqdm.write(message, file=self._stream)
            return
        current, total, description = match.groups()
        current_value = int(current)
        total_value = int(total)
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


__all__ = ["ProgressReporter"]
