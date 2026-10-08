"""UTC wall-clock reads and the serialized timestamp formats built from them.

Each helper returns exactly the string its call site wrote before this module
existed. Those strings are persisted (``run.json``, ``sources.json``,
``failures.jsonl`` and run directory names), so do not reformat them:

* ``utc_iso`` keeps ``isoformat()`` precision, so microseconds appear only when
  they are non-zero.
* ``utc_iso_seconds`` always writes whole seconds.
* ``utc_run_id`` writes the compact ``YYYYMMDDTHHMMSSZ`` run identifier.
"""

from __future__ import annotations

import datetime as dt

_RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def utc_now() -> dt.datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return dt.datetime.now(tz=dt.UTC)


def utc_iso() -> str:
    """Return the current UTC time in ``isoformat()`` form, with a ``+00:00`` offset."""
    return utc_now().isoformat()


def utc_iso_seconds(moment: dt.datetime | None = None) -> str:
    """Return ``moment`` (default: now) truncated to whole seconds, ``isoformat()`` form."""
    if moment is None:
        moment = utc_now()
    return moment.replace(microsecond=0).isoformat()


def utc_run_id() -> str:
    """Return the current UTC time as a run identifier such as ``20260314T150926Z``."""
    return utc_now().strftime(_RUN_ID_FORMAT)


__all__ = ["utc_iso", "utc_iso_seconds", "utc_now", "utc_run_id"]
