import json
import tempfile
import unittest
from pathlib import Path
from typing import List

from webhook_bridge.bridge import InvalidEvent, WebhookBridge, parse_event
from webhook_bridge.demo import DemoDownstream, sample_event
from webhook_bridge.store import EventStore


class TestWebhookBridge(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "bridge.db"
        self.downstream = DemoDownstream()
        self.sleeps: List[float] = []

    def make_bridge(self) -> WebhookBridge:
        return WebhookBridge(
            EventStore(self.db_path),
            self.downstream,
            max_attempts=3,
            base_delay=0.05,
            sleep=self.sleeps.append,
        )

    def test_duplicate_processed_once(self) -> None:
        bridge = self.make_bridge()
        first = bridge.handle(sample_event(1))
        second = bridge.handle(sample_event(1))
        self.assertEqual((first.status, first.attempts), ("processed", 1))
        self.assertEqual((second.status, second.attempts), ("duplicate", 0))
        self.assertEqual(second.previous_status, "processed")
        self.assertEqual(self.downstream.calls["evt-1"], 1)

    def test_duplicate_detected_after_restart(self) -> None:
        self.make_bridge().handle(sample_event(1))
        restarted = self.make_bridge()  # new store and bridge on the same SQLite file
        self.assertEqual(restarted.handle(sample_event(1)).status, "duplicate")
        self.assertEqual(self.downstream.calls["evt-1"], 1)

    def test_transient_failure_succeeds_after_retry(self) -> None:
        outcome = self.make_bridge().handle(sample_event(2, "fail_once"))
        self.assertEqual((outcome.status, outcome.attempts), ("processed", 2))
        self.assertEqual(self.sleeps, [0.05])
        self.assertEqual(EventStore(self.db_path).list_dead_letters(), [])

    def test_permanent_failure_lands_in_dlq(self) -> None:
        bridge = self.make_bridge()
        outcome = bridge.handle(sample_event(3, "always_fail"))
        self.assertEqual((outcome.status, outcome.attempts), ("dead_lettered", 3))
        self.assertEqual(outcome.last_error, "RuntimeError: downstream unavailable (demo)")
        self.assertEqual(self.sleeps, [0.05, 0.1])

        store = EventStore(self.db_path)
        [entry] = store.list_dead_letters()
        self.assertEqual(entry["event_id"], "evt-3")
        self.assertEqual(entry["attempts"], 3)
        self.assertEqual(json.loads(entry["payload"]), sample_event(3, "always_fail"))
        self.assertEqual(store.status_counts(), {"dead_lettered": 1})

        # Sending the same event again does not run the handler again.
        again = bridge.handle(sample_event(3, "always_fail"))
        self.assertEqual((again.status, again.previous_status), ("duplicate", "dead_lettered"))
        self.assertEqual(self.downstream.calls["evt-3"], 3)

    def test_parse_event_rejects_invalid_payloads(self) -> None:
        self.assertEqual(parse_event(json.dumps(sample_event(4)).encode()), sample_event(4))
        bad_bodies = [
            b"not json",
            b"\xff\xfe",
            b"[]",
            b'{"type": "ticket.created", "data": {}}',
            b'{"event_id": "", "type": "ticket.created", "data": {}}',
            json.dumps({"event_id": "e" * 201, "type": "t", "data": {}}).encode(),
            b'{"event_id": "evt-4", "data": {}}',
            b'{"event_id": "evt-4", "type": "ticket.created", "data": "x"}',
        ]
        for body in bad_bodies:
            with self.subTest(body=body[:40]):
                with self.assertRaises(InvalidEvent):
                    parse_event(body)


if __name__ == "__main__":
    unittest.main()
