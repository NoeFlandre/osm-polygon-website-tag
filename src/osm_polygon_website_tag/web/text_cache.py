"""Persistent run-owned cache for website text extraction results."""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from osm_polygon_website_tag.contracts.text_schema import TEXT_STATUSES

_CACHE_BUSY_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_COUNT = 5
_LOCK_RETRY_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class CachedText:
    """One normalized URL's latest extraction result."""

    url: str
    status: str
    text: str | None
    word_count: int | None
    final_url: str | None
    message: str | None
    attempt_count: int
    last_attempt_at: str
    trafilatura_version: str | None
    invocation_id: str


class TextCache:
    """SQLite-backed cache with retry-on-next-invocation semantics."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, timeout=_CACHE_BUSY_TIMEOUT_SECONDS)
        try:
            self._create_schema()
        except sqlite3.DatabaseError as error:
            self._db.close()
            if not _is_corruption_error(error):
                raise
            _quarantine_corrupt_database(path)
            self._db = sqlite3.connect(path, timeout=_CACHE_BUSY_TIMEOUT_SECONDS)
            self._create_schema()

    def _create_schema(self) -> None:
        _retry_locked(
            lambda: self._db.execute(
                """CREATE TABLE IF NOT EXISTS website_text (
                url TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                text TEXT,
                word_count INTEGER,
                final_url TEXT,
                message TEXT,
                attempt_count INTEGER NOT NULL,
                last_attempt_at TEXT NOT NULL,
                trafilatura_version TEXT,
                invocation_id TEXT NOT NULL
                )"""
            )
        )
        _retry_locked(self._db.commit)

    def get_reusable(self, url: str, *, invocation_id: str) -> CachedText | None:
        """Return a success or a result already attempted in this invocation."""
        value = self._get(url)
        if value is None:
            return None
        if value.status == "success" or value.invocation_id == invocation_id:
            return value
        return None

    def record(self, value: CachedText, *, invocation_id: str) -> CachedText:
        """Persist one attempt and return its canonical stored representation."""
        if value.status not in TEXT_STATUSES - {"absent", "pending"}:
            raise ValueError(f"invalid cache status: {value.status!r}")
        prior = self._get(value.url)
        stored = replace(
            value,
            attempt_count=1 if prior is None else prior.attempt_count + 1,
            last_attempt_at=dt.datetime.now(tz=dt.UTC).isoformat(),
            invocation_id=invocation_id,
        )
        _retry_locked(
            lambda: self._db.execute(
                """INSERT INTO website_text VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                 status=excluded.status,
                 text=excluded.text,
                 word_count=excluded.word_count,
                 final_url=excluded.final_url,
                 message=excluded.message,
                 attempt_count=excluded.attempt_count,
                 last_attempt_at=excluded.last_attempt_at,
                 trafilatura_version=excluded.trafilatura_version,
                 invocation_id=excluded.invocation_id""",
                (
                    stored.url,
                    stored.status,
                    stored.text,
                    stored.word_count,
                    stored.final_url,
                    stored.message,
                    stored.attempt_count,
                    stored.last_attempt_at,
                    stored.trafilatura_version,
                    stored.invocation_id,
                ),
            ),
        )
        _retry_locked(self._db.commit)
        return stored

    def _get(self, url: str) -> CachedText | None:
        row = _retry_locked(
            lambda: self._db.execute(
                """SELECT url, status, text, word_count, final_url, message,
                      attempt_count, last_attempt_at, trafilatura_version,
                      invocation_id
                   FROM website_text WHERE url=?""",
                (url,),
            ).fetchone()
        )
        if row is None:
            return None
        return CachedText(
            url=str(row[0]),
            status=str(row[1]),
            text=row[2],
            word_count=None if row[3] is None else int(row[3]),
            final_url=row[4],
            message=row[5],
            attempt_count=int(row[6]),
            last_attempt_at=str(row[7]),
            trafilatura_version=row[8],
            invocation_id=str(row[9]),
        )

    def close(self) -> None:
        self._db.close()


def _retry_locked[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    """Retry a short-lived SQLite writer lock with bounded exponential backoff."""
    for attempt in range(_LOCK_RETRY_COUNT + 1):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if not _is_locked_error(error) or attempt == _LOCK_RETRY_COUNT:
                raise
            time.sleep(_LOCK_RETRY_DELAY_SECONDS * (2**attempt))
    raise AssertionError("unreachable")


def _is_locked_error(error: sqlite3.OperationalError) -> bool:
    """Return whether SQLite rejected an operation because another writer is active."""
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


def _is_corruption_error(error: sqlite3.DatabaseError) -> bool:
    """Return whether SQLite identified an unreadable database image."""
    message = str(error).lower()
    return "malformed" in message or "not a database" in message


def _quarantine_corrupt_database(path: Path) -> Path:
    """Move a corrupt cache and its SQLite sidecars out of the active path."""
    token = f"{dt.datetime.now(tz=dt.UTC):%Y%m%dT%H%M%S%fZ}-{uuid4().hex}"
    quarantine = path.with_name(f"{path.name}.corrupt-{token}")
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.replace(Path(f"{quarantine}{suffix}"))
    return quarantine


__all__ = ["CachedText", "TextCache"]
