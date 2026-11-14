"""The demo, send and dlq commands, run in-process."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Tuple
from unittest import mock

from webhook_bridge.__main__ import main
from webhook_bridge.bridge import WebhookBridge
from webhook_bridge.demo import DemoDownstream, run_demo
from webhook_bridge.server import WebhookServer, serve_in_background
from webhook_bridge.signature import load_secret
from webhook_bridge.store import EventStore

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class TestCommands(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "demo.db"

    def run_main(self, *argv: str) -> Tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_demo_passes_all_checks_and_dlq_lists_the_failed_event(self) -> None:
        out = io.StringIO()
        self.assertEqual(run_demo(self.db_path, out=out), 0)
        self.assertIn("result                  : 10/10 checks passed", out.getvalue())

        # Running the demo again recreates the database, so the result is the same.
        self.assertEqual(run_demo(self.db_path, out=io.StringIO()), 0)

        code, text = self.run_main("dlq", "--db", str(self.db_path))
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith(f"1 dead-lettered event(s) in {self.db_path}"))
        self.assertIn("evt-1004  ticket.created  attempts=3", text)

    def test_demo_returns_1_when_a_check_does_not_match(self) -> None:
        out = io.StringIO()
        with mock.patch("webhook_bridge.demo.EXPECTED_DLQ", ["evt-9999"]):
            self.assertEqual(run_demo(self.db_path, out=out), 1)
        self.assertIn("result                  : 9/10 checks passed", out.getvalue())

    def test_send_posts_sample_files_to_the_receiver(self) -> None:
        secret, _ = load_secret()  # send and the server read the same secret
        bridge = WebhookBridge(EventStore(self.db_path), DemoDownstream(), max_attempts=3, base_delay=0.01)
        with serve_in_background(WebhookServer(0, bridge, secret, quiet=True)) as server:
            port = str(server.port)
            code, text = self.run_main("send", str(SAMPLES / "event_ok.json"), "--port", port)
            self.assertEqual(code, 0)
            self.assertIn('HTTP 200 {"status": "processed", "event_id": "evt-2001", "attempts": 1}', text)
            code, text = self.run_main("send", str(SAMPLES / "event_always_fail.json"), "--port", port)
            self.assertEqual(code, 0)
            self.assertIn('HTTP 202 {"status": "dead_lettered", "event_id": "evt-2002"', text)
        code, text = self.run_main("dlq", "--db", str(self.db_path))
        self.assertIn("evt-2002  ticket.created  attempts=3", text)

    def test_dlq_reports_missing_database(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = self.run_main("dlq", "--db", str(self.db_path))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err.getvalue())
        self.assertFalse(self.db_path.exists())  # listing must not create an empty database


if __name__ == "__main__":
    unittest.main()
