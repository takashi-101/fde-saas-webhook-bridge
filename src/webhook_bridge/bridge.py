"""Idempotent SaaS/Webhook ingestion bridge with schema validation, retry, and DLQ."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    source_system: str
    payload: Dict[str, str]


class WebhookBridge:
    """Guarantees at-most-once side effects via idempotency keys and dead-letter queue."""

    REQUIRED_FIELDS = ("ticket_id", "summary")

    def __init__(self, db_path: Path | str, max_retries: int = 3) -> None:
        self.db_path = Path(db_path)
        self.max_retries = max_retries
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    source_system TEXT NOT NULL,
                    ticket_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    event_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL
                );
                """
            )

    def ingest(
        self,
        event: WebhookEvent,
        downstream_handler: Callable[[WebhookEvent], None],
    ) -> str:
        # 1. Schema validation
        for req in self.REQUIRED_FIELDS:
            if not event.payload.get(req):
                with self._connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO dead_letter_queue (event_id, reason) VALUES (?, ?)",
                        (event.event_id, f"missing_field:{req}"),
                    )
                return "REJECTED_SCHEMA"

        # 2. Idempotency check (prevent duplicate execution on SaaS webhook retries)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
        if existing:
            return "SKIPPED_DUPLICATE"

        # 3. Retry loop with dead-letter fallback
        last_err = ""
        for _ in range(self.max_retries):
            try:
                downstream_handler(event)
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO processed_events (event_id, source_system, ticket_id) VALUES (?, ?, ?)",
                        (event.event_id, event.source_system, event.payload["ticket_id"]),
                    )
                return "PROCESSED"
            except RuntimeError as exc:
                last_err = str(exc)

        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dead_letter_queue (event_id, reason) VALUES (?, ?)",
                (event.event_id, f"max_retries_exceeded:{last_err}"),
            )
        return "SENT_TO_DLQ"

    def fetch_dlq(self) -> List[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM dead_letter_queue").fetchall()]
