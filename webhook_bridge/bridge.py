"""Event validation, idempotent processing, bounded retry with backoff, and DLQ fallback."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Optional

from .store import EventStore

Event = Dict[str, Any]
Handler = Callable[[Event], None]

MAX_EVENT_ID_LENGTH = 200


class InvalidEvent(ValueError):
    """The body is not a JSON object with the required fields."""


def parse_event(body: bytes) -> Event:
    """Parse and validate {"event_id": str, "type": str, "data": object}."""
    try:
        event = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InvalidEvent("body must be UTF-8 encoded JSON") from None
    if not isinstance(event, dict):
        raise InvalidEvent("body must be a JSON object")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > MAX_EVENT_ID_LENGTH:
        raise InvalidEvent(
            f"event_id must be a non-empty string of at most {MAX_EVENT_ID_LENGTH} characters"
        )
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise InvalidEvent("type must be a non-empty string")
    if not isinstance(event.get("data"), dict):
        raise InvalidEvent("data must be a JSON object")
    return event


@dataclass(frozen=True)
class Outcome:
    status: str  # processed / duplicate / dead_lettered
    event_id: str
    attempts: int  # handler calls made for this delivery (0 for a duplicate)
    last_error: Optional[str] = None
    previous_status: Optional[str] = None  # duplicate only: status stored for the first delivery

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


class WebhookBridge:
    """Runs the handler at most once per event_id, retrying failures before dead-lettering."""

    def __init__(
        self,
        store: EventStore,
        handler: Handler,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if base_delay < 0:
            raise ValueError("base_delay must not be negative")
        self.store = store
        self.handler = handler
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._sleep = sleep

    def backoff(self, attempt: int) -> float:
        """Seconds to wait after the given failed attempt (1-based): base_delay * 2**(attempt-1)."""
        return self.base_delay * (2 ** (attempt - 1))

    def handle(self, event: Event) -> Outcome:
        event_id = event["event_id"]
        previous = self.store.claim(event_id, event["type"])
        if previous is not None:
            return Outcome("duplicate", event_id, 0, previous_status=previous)

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.handler(event)
            except Exception as exc:  # every handler failure is retried, then dead-lettered
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_attempts:
                    self._sleep(self.backoff(attempt))
            else:
                self.store.mark_processed(event_id, attempt)
                return Outcome("processed", event_id, attempt)

        self.store.dead_letter(event, self.max_attempts, last_error)
        return Outcome("dead_lettered", event_id, self.max_attempts, last_error=last_error)
