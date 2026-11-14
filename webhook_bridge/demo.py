"""End-to-end demo: start the receiver, post sample notifications to it with urllib, check the results."""

from __future__ import annotations

import json
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Union

from .bridge import Event, WebhookBridge
from .client import post_webhook
from .server import WebhookServer, serve_in_background
from .signature import SECRET_ENV, load_secret, sign
from .store import EventStore

DEMO_MAX_ATTEMPTS = 3
DEMO_BASE_DELAY = 0.05  # seconds: waits 0.05 s and 0.1 s between the 3 attempts


class DemoDownstream:
    """Stand-in for the system that acts on a notification. It only counts calls.

    data.simulate == "fail_once":   the first call for that event_id raises, later calls succeed.
    data.simulate == "always_fail": every call raises.
    Anything else succeeds.
    """

    def __init__(self) -> None:
        self.calls: Counter = Counter()
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        event_id = event["event_id"]
        with self._lock:
            self.calls[event_id] += 1
            count = self.calls[event_id]
        mode = event["data"].get("simulate")
        if mode == "always_fail":
            raise RuntimeError("downstream unavailable (demo)")
        if mode == "fail_once" and count == 1:
            raise RuntimeError("temporary failure (demo)")


def sample_event(number: int, simulate: Optional[str] = None) -> Event:
    """Fictitious notification shape used throughout the demo and tests."""
    data: Dict[str, Any] = {"ticket_id": f"T-{number}", "subject": "sample inquiry (fictitious)"}
    if simulate:
        data["simulate"] = simulate
    return {"event_id": f"evt-{number}", "type": "ticket.created", "data": data}


@dataclass(frozen=True)
class Case:
    label: str
    event: Event
    signing: str  # "shared" (correct secret) / "wrong" (different secret) / "none" (no header)
    expect_code: int
    expect_status: str
    expect_attempts: Optional[int]
    after_restart: bool = False


CASES: List[Case] = [
    Case("valid signature", sample_event(1001), "shared", 200, "processed", 1),
    Case("same event again", sample_event(1001), "shared", 200, "duplicate", 0),
    Case("signed with wrong secret", sample_event(1002), "wrong", 401, "invalid_signature", None),
    Case("no signature header", sample_event(1002), "none", 401, "missing_signature", None),
    Case("fails once, then retried", sample_event(1003, "fail_once"), "shared", 200, "processed", 2),
    Case("always fails", sample_event(1004, "always_fail"), "shared", 202, "dead_lettered", 3),
    Case("same event after restart", sample_event(1001), "shared", 200, "duplicate", 0, after_restart=True),
]
EXPECTED_CALLS = {"evt-1001": 1, "evt-1002": 0, "evt-1003": 2, "evt-1004": 3}
EXPECTED_COUNTS = {"dead_lettered": 1, "processed": 2}
EXPECTED_DLQ = ["evt-1004"]

ROW = "{:>2}  {:<26}{:<10}{:>4}  {:<19}{:>8}  {}"


def _signature(secret: str, body: bytes, signing: str) -> Optional[str]:
    if signing == "shared":
        return sign(secret, body)
    if signing == "wrong":
        return sign(secret + "-wrong", body)
    return None


def _remove_db(db_path: Path) -> None:
    for path in (db_path, db_path.with_name(db_path.name + "-journal")):
        if path.exists():
            path.unlink()


def _start(db_path: Path, downstream: DemoDownstream, secret: str) -> WebhookServer:
    bridge = WebhookBridge(
        EventStore(db_path),
        downstream,
        max_attempts=DEMO_MAX_ATTEMPTS,
        base_delay=DEMO_BASE_DELAY,
    )
    return WebhookServer(0, bridge, secret, quiet=True)  # port 0: the OS picks a free port


def run_demo(db_path: Union[str, Path] = "demo.db", out: Optional[TextIO] = None) -> int:
    """Run every case, print a summary, and return 0 only if all checks match."""
    out = out or sys.stdout
    db_path = Path(db_path)

    def say(line: str = "") -> None:
        print(line, file=out)

    _remove_db(db_path)
    secret, from_env = load_secret()
    downstream = DemoDownstream()
    checks: List[bool] = []

    say("webhook-bridge demo")
    say(f"  database : {db_path} (deleted and recreated for this run)")
    say("  server   : http.server bound to 127.0.0.1, ephemeral port, POST /webhook")
    say("  secret   : " + (f"from {SECRET_ENV}" if from_env else f"built-in demo value ({SECRET_ENV} is not set)"))
    say(f"  retry    : up to {DEMO_MAX_ATTEMPTS} attempts, waits {DEMO_BASE_DELAY} s x 2^(n-1) between them")
    say()
    say(ROW.format("#", "case", "event_id", "HTTP", "status", "attempts", "check"))

    for restarted in (False, True):
        if restarted:
            say("    -- server stopped and started again on the same SQLite file --")
        with serve_in_background(_start(db_path, downstream, secret)) as server:
            for number, case in enumerate(CASES, start=1):
                if case.after_restart != restarted:
                    continue
                body = json.dumps(case.event).encode("utf-8")
                code, reply = post_webhook(server.port, body, _signature(secret, body, case.signing))
                status, attempts = reply.get("status"), reply.get("attempts")
                ok = (code, status, attempts) == (case.expect_code, case.expect_status, case.expect_attempts)
                checks.append(ok)
                say(ROW.format(
                    number, case.label, case.event["event_id"], code, str(status),
                    "-" if attempts is None else attempts, "ok" if ok else "MISMATCH",
                ))

    store = EventStore(db_path)
    calls = {event_id: downstream.calls.get(event_id, 0) for event_id in EXPECTED_CALLS}
    counts = store.status_counts()
    dead = store.list_dead_letters()

    def mark(ok: bool) -> str:
        checks.append(ok)
        return "ok" if ok else "MISMATCH"

    say()
    say("summary")
    say("  handler calls per event : "
        + ", ".join(f"{k}={v}" for k, v in calls.items()) + f"  [{mark(calls == EXPECTED_CALLS)}]")
    say("  events table by status  : "
        + ", ".join(f"{k}={v}" for k, v in counts.items()) + f"  [{mark(counts == EXPECTED_COUNTS)}]")
    say(f"  dead-letter queue       : {len(dead)} event(s)"
        + f"  [{mark([d['event_id'] for d in dead] == EXPECTED_DLQ)}]")
    for d in dead:
        say(f"    {d['event_id']}  {d['event_type']}  attempts={d['attempts']}  last_error={d['last_error']}")
    passed = sum(checks)
    say(f"  result                  : {passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1
