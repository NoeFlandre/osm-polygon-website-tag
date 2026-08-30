"""SQLite-backed extraction candidate ledger.

The ledger is per-attempt scratch state: it batches SQLite mutations behind a
bounded commit interval to amortize journal ``fsync`` cost during extraction,
flushes any pending mutations on :meth:`CandidateLedger.close`, and is deleted
after successful extraction. It is **not** a resume checkpoint.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Internal default: commit after this many ledger mutations. The extraction hot
# path calls upsert()/mark_area_seen() once per qualifying object, and every
# commit forces an fsync of the SQLite journal. Batching amortizes that cost
# while keeping memory bounded and preserving all read semantics. This is not
# user-facing configuration; the value is deliberately conservative.
DEFAULT_COMMIT_BATCH_SIZE = 4096


def _candidate_payload(
    tags_json: str,
    osm_version: int,
    osm_timestamp: str,
    candidate_kind: str,
) -> dict[str, Any]:
    """Decode the candidate columns shared by ledger read queries."""
    return {
        "tags": json.loads(tags_json),
        "osm_version": int(osm_version),
        "osm_timestamp": dt.datetime.fromisoformat(osm_timestamp),
        "candidate_kind": candidate_kind,
    }


class CandidateLedger:
    """Persist candidates and area callbacks without source-sized Python state."""

    def __init__(
        self,
        path: Path,
        *,
        commit_batch_size: int = DEFAULT_COMMIT_BATCH_SIZE,
    ) -> None:
        if commit_batch_size <= 0:
            raise ValueError(
                f"commit_batch_size must be a positive integer, got {commit_batch_size!r}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._commit_batch_size = commit_batch_size
        self._pending_mutations = 0
        self._closed = False
        # The ledger is per-attempt scratch state, not a resume checkpoint.
        # A prior interruption may leave it behind; retry must start clean.
        path.unlink(missing_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE candidates (
                osm_type TEXT NOT NULL,
                osm_id INTEGER NOT NULL,
                tags_json TEXT NOT NULL,
                osm_version INTEGER NOT NULL,
                osm_timestamp TEXT NOT NULL,
                candidate_kind TEXT NOT NULL,
                area_seen INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (osm_type, osm_id)
            )"""
        )

    def upsert(
        self,
        osm_type: str,
        osm_id: int,
        tags: dict[str, str],
        osm_version: int,
        osm_timestamp: dt.datetime,
        candidate_kind: str,
    ) -> None:
        self._db.execute(
            """INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(osm_type, osm_id) DO UPDATE SET
                 tags_json=excluded.tags_json,
                 osm_version=excluded.osm_version,
                 osm_timestamp=excluded.osm_timestamp,
                 candidate_kind=excluded.candidate_kind
               WHERE excluded.osm_version > candidates.osm_version""",
            (
                osm_type,
                osm_id,
                json.dumps(tags, sort_keys=True, separators=(",", ":")),
                osm_version,
                osm_timestamp.isoformat(),
                candidate_kind,
            ),
        )
        self._note_mutation()

    def mark_area_seen(self, osm_type: str, osm_id: int) -> bool:
        row = self._db.execute(
            "SELECT area_seen FROM candidates WHERE osm_type=? AND osm_id=?",
            (osm_type, osm_id),
        ).fetchone()
        if row is None:
            return False
        if bool(row[0]):
            raise ValueError("duplicate_area_callback")
        self._db.execute(
            "UPDATE candidates SET area_seen=1 WHERE osm_type=? AND osm_id=?",
            (osm_type, osm_id),
        )
        self._note_mutation()
        return True

    def _note_mutation(self) -> None:
        """Count one successful mutation and commit when the batch is full."""
        self._pending_mutations += 1
        if self._pending_mutations >= self._commit_batch_size:
            self._flush()

    def _flush(self) -> None:
        """Commit pending mutations, if any."""
        if self._pending_mutations > 0:
            self._db.commit()
            self._pending_mutations = 0

    def get(self, osm_type: str, osm_id: int) -> dict[str, Any] | None:
        row = self._db.execute(
            """SELECT tags_json, osm_version, osm_timestamp, candidate_kind
               FROM candidates WHERE osm_type=? AND osm_id=?""",
            (osm_type, osm_id),
        ).fetchone()
        if row is None:
            return None
        return _candidate_payload(row[0], row[1], row[2], row[3])

    def missing_areas(self) -> Iterator[tuple[str, int, dict[str, Any]]]:
        cursor = self._db.execute(
            """SELECT osm_type, osm_id, tags_json, osm_version,
                      osm_timestamp, candidate_kind
               FROM candidates WHERE area_seen=0 ORDER BY osm_type, osm_id"""
        )
        for row in cursor:
            yield (
                row[0],
                int(row[1]),
                _candidate_payload(row[2], row[3], row[4], row[5]),
            )

    def close(self) -> None:
        # Extraction calls close() on both the success and failure paths; the
        # guard makes repeated calls a no-op. Pending mutations are flushed so
        # the same-connection reads during extraction remain durable on disk.
        if self._closed:
            return
        self._flush()
        self._db.close()
        self._closed = True
