import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from webhook_bridge import WebhookBridge, WebhookEvent


class TestWebhookBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = WebhookBridge(Path(self.tmp.name) / "bridge.db", max_retries=3)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_idempotency_prevents_duplicate_execution(self) -> None:
        calls = 0

        def handler(_: WebhookEvent) -> None:
            nonlocal calls
            calls += 1

        ev = WebhookEvent("evt-1", "kintone", {"ticket_id": "T-10", "summary": "照会"})
        self.assertEqual(self.bridge.ingest(ev, handler), "PROCESSED")
        self.assertEqual(self.bridge.ingest(ev, handler), "SKIPPED_DUPLICATE")
        self.assertEqual(calls, 1)

    def test_retries_transient_error_then_succeeds(self) -> None:
        attempts = 0

        def flaky(_: WebhookEvent) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError("temporary timeout")

        ev = WebhookEvent("evt-2", "form", {"ticket_id": "T-20", "summary": "相談"})
        self.assertEqual(self.bridge.ingest(ev, flaky), "PROCESSED")
        self.assertEqual(attempts, 2)

    def test_invalid_schema_goes_to_dlq(self) -> None:
        ev = WebhookEvent("evt-3", "form", {"ticket_id": "T-30"})  # missing summary
        self.assertEqual(self.bridge.ingest(ev, lambda _: None), "REJECTED_SCHEMA")
        self.assertEqual(len(self.bridge.fetch_dlq()), 1)


if __name__ == "__main__":
    unittest.main()
