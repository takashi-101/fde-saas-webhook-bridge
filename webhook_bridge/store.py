"""SQLite storage for idempotency records and the dead-letter queue (DLQ)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    status      TEXT NOT NULL,  -- processing / processed / dead_lettered
    attempts    INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    event_id         TEXT PRIMARY KEY,
    event_type       TEXT NOT NULL,
    payload          TEXT NOT NULL,
    attempts         INTEGER NOT NULL,
    last_error       TEXT NOT NULL,
    dead_lettered_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EventStore:
    """Every method opens its own connection and closes it before returning."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on error, and always close the connection."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def claim(self, event_id: str, event_type: str) -> Optional[str]:
        """Record the event as 'processing'.

        Returns None if this call recorded it (the caller must process it), or the
        status already stored for that event_id (the caller must not process it).
        The INSERT OR IGNORE on the primary key makes the check-and-record atomic.
        """
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events"
                " (event_id, event_type, status, attempts, received_at, updated_at)"
                " VALUES (?, ?, 'processing', 0, ?, ?)",
                (event_id, event_type, now, now),
            )
            if cur.rowcount == 1:
                return None
            row = conn.execute(
                "SELECT status FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return str(row["status"])

    def mark_processed(self, event_id: str, attempts: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE events SET status = 'processed', attempts = ?, updated_at = ?"
                " WHERE event_id = ?",
                (attempts, _now(), event_id),
            )

    def dead_letter(self, event: Dict[str, Any], attempts: int, last_error: str) -> None:
        """Mark the event as dead-lettered and copy it into the DLQ in one transaction."""
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE events SET status = 'dead_lettered', attempts = ?, updated_at = ?"
                " WHERE event_id = ?",
                (attempts, now, event["event_id"]),
            )
            conn.execute(
                "INSERT OR REPLACE INTO dead_letter_queue"
                " (event_id, event_type, payload, attempts, last_error, dead_lettered_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event["event_id"],
                    event["type"],
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                    attempts,
                    last_error,
                    now,
                ),
            )

    def list_dead_letters(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, event_type, attempts, last_error, dead_lettered_at, payload"
                " FROM dead_letter_queue ORDER BY rowid"
            ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self) -> Dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}
