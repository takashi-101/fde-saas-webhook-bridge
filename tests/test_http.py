"""HTTP-level tests against the real http.server receiver on an ephemeral port."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from webhook_bridge.bridge import WebhookBridge
from webhook_bridge.client import post_webhook
from webhook_bridge.demo import DemoDownstream, sample_event
from webhook_bridge.server import MAX_BODY_BYTES, WebhookServer, serve_in_background
from webhook_bridge.signature import sign
from webhook_bridge.store import EventStore

SECRET = "http-test-secret-fictitious"


class TestHttpServer(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "bridge.db"
        self.downstream = DemoDownstream()
        bridge = WebhookBridge(EventStore(self.db_path), self.downstream, max_attempts=3, base_delay=0.01)
        running = serve_in_background(WebhookServer(0, bridge, SECRET, quiet=True))
        self.server = running.__enter__()
        self.addCleanup(running.__exit__, None, None, None)  # runs before tmp.cleanup

    def post(self, event: Any, secret: Optional[str] = SECRET, path: str = "/webhook") -> Tuple[int, Dict[str, Any]]:
        body = event if isinstance(event, bytes) else json.dumps(event).encode("utf-8")
        signature = sign(secret, body) if secret is not None else None
        return post_webhook(self.server.port, body, signature, path=path)

    def test_binds_loopback_on_ephemeral_port(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.server.port, 0)

    def test_valid_signature_processed(self) -> None:
        code, reply = self.post(sample_event(1))
        self.assertEqual(code, 200)
        self.assertEqual(reply, {"status": "processed", "event_id": "evt-1", "attempts": 1})

    def test_invalid_signature_rejected_and_not_recorded(self) -> None:
        code, reply = self.post(sample_event(1), secret="wrong-secret")
        self.assertEqual((code, reply["status"]), (401, "invalid_signature"))
        self.assertEqual(self.downstream.calls["evt-1"], 0)
        self.assertEqual(EventStore(self.db_path).status_counts(), {})

    def test_missing_signature_rejected(self) -> None:
        code, reply = self.post(sample_event(1), secret=None)
        self.assertEqual((code, reply["status"]), (401, "missing_signature"))
        self.assertEqual(self.downstream.calls["evt-1"], 0)

    def test_duplicate_processed_once(self) -> None:
        self.post(sample_event(1))
        code, reply = self.post(sample_event(1))
        self.assertEqual((code, reply["status"], reply["previous_status"]), (200, "duplicate", "processed"))
        self.assertEqual(self.downstream.calls["evt-1"], 1)

    def test_transient_failure_succeeds_after_retry(self) -> None:
        code, reply = self.post(sample_event(2, "fail_once"))
        self.assertEqual((code, reply["status"], reply["attempts"]), (200, "processed", 2))

    def test_permanent_failure_lands_in_dlq(self) -> None:
        code, reply = self.post(sample_event(3, "always_fail"))
        self.assertEqual((code, reply["status"], reply["attempts"]), (202, "dead_lettered", 3))
        self.assertEqual(reply["last_error"], "RuntimeError: downstream unavailable (demo)")
        dead = EventStore(self.db_path).list_dead_letters()
        self.assertEqual([d["event_id"] for d in dead], ["evt-3"])

    def test_signed_but_invalid_payload_returns_400(self) -> None:
        code, reply = self.post(b"not json")
        self.assertEqual((code, reply["status"]), (400, "invalid_payload"))
        code, reply = self.post({"event_id": "evt-5", "type": "ticket.created"})
        self.assertEqual((code, reply["error"]), (400, "data must be a JSON object"))

    def test_unknown_path_returns_404_and_get_returns_501(self) -> None:
        code, reply = self.post(sample_event(1), path="/other")
        self.assertEqual((code, reply["status"]), (404, "not_found"))
        self.assertEqual(self.downstream.calls["evt-1"], 0)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            conn.request("GET", "/webhook")
            response = conn.getresponse()
            response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 501)

    def raw_post_without_body(self, content_length: str) -> Tuple[int, Dict[str, Any]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            conn.putrequest("POST", "/webhook")
            conn.putheader("Content-Length", content_length)
            conn.endheaders()  # send the header only; no body follows
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_oversized_body_returns_413_without_reading_it(self) -> None:
        code, reply = self.raw_post_without_body(str(MAX_BODY_BYTES + 1))
        self.assertEqual((code, reply["status"]), (413, "payload_too_large"))

    def test_invalid_content_length_returns_400(self) -> None:
        for value in ("abc", "-1"):
            with self.subTest(value=value):
                code, reply = self.raw_post_without_body(value)
                self.assertEqual((code, reply["status"]), (400, "invalid_content_length"))

    def test_concurrent_duplicates_processed_once(self) -> None:
        calls: List[str] = []
        lock = threading.Lock()

        def slow_handler(event: Dict[str, Any]) -> None:
            with lock:
                calls.append(event["event_id"])
            time.sleep(0.2)  # keep the first delivery in progress while the others arrive

        bridge = WebhookBridge(EventStore(self.db_path), slow_handler, max_attempts=1, base_delay=0)
        body = json.dumps(sample_event(7)).encode("utf-8")
        results: List[Tuple[int, Dict[str, Any]]] = []
        with serve_in_background(WebhookServer(0, bridge, SECRET, quiet=True)) as server:
            def send() -> None:
                result = post_webhook(server.port, body, sign(SECRET, body))
                with lock:
                    results.append(result)

            threads = [threading.Thread(target=send) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        statuses = sorted(reply["status"] for _, reply in results)
        self.assertEqual(statuses, ["duplicate"] * 9 + ["processed"])
        self.assertEqual(calls, ["evt-7"])


if __name__ == "__main__":
    unittest.main()
