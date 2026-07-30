"""SQLite-backed extraction candidate ledger."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class CandidateLedger:
    """Persist candidates and area callbacks without source-sized Python state."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
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
        self._db.commit()

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
        self._db.commit()
        return True

    def get(self, osm_type: str, osm_id: int) -> dict[str, Any] | None:
        row = self._db.execute(
            """SELECT tags_json, osm_version, osm_timestamp, candidate_kind
               FROM candidates WHERE osm_type=? AND osm_id=?""",
            (osm_type, osm_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "tags": json.loads(row[0]),
            "osm_version": int(row[1]),
            "osm_timestamp": dt.datetime.fromisoformat(row[2]),
            "candidate_kind": row[3],
        }

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
                {
                    "tags": json.loads(row[2]),
                    "osm_version": int(row[3]),
                    "osm_timestamp": dt.datetime.fromisoformat(row[4]),
                    "candidate_kind": row[5],
                },
            )

    def close(self) -> None:
        self._db.close()
