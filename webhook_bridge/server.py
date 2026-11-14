"""Webhook receiver on http.server. It binds 127.0.0.1 only."""

from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator

from .bridge import InvalidEvent, WebhookBridge, parse_event
from .signature import SIGNATURE_HEADER, verify

HOST = "127.0.0.1"
WEBHOOK_PATH = "/webhook"
MAX_BODY_BYTES = 64 * 1024


class WebhookServer(ThreadingHTTPServer):
    # Non-daemon request threads: server_close() waits for requests in flight.
    daemon_threads = False
    # On Windows, SO_REUSEADDR would let this bind a port another process is already using.
    allow_reuse_address = sys.platform != "win32"
    request_queue_size = 64  # listen backlog (socketserver's default is 5)

    def __init__(self, port: int, bridge: WebhookBridge, secret: str, *, quiet: bool = False) -> None:
        self.bridge = bridge
        self.secret = secret
        self.quiet = quiet
        super().__init__((HOST, port), WebhookRequestHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}{WEBHOOK_PATH}"


class WebhookRequestHandler(BaseHTTPRequestHandler):
    server: WebhookServer
    server_version = "webhook-bridge"

    def do_POST(self) -> None:  # noqa: N802 (name required by BaseHTTPRequestHandler)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._reply(400, {"status": "invalid_content_length"})
            return
        if length > MAX_BODY_BYTES:
            # The body is not read, so the connection is closed after the reply.
            self.close_connection = True
            self._reply(413, {"status": "payload_too_large", "max_bytes": MAX_BODY_BYTES})
            return
        body = self.rfile.read(length)

        if self.path != WEBHOOK_PATH:
            self._reply(404, {"status": "not_found"})
            return

        signature = self.headers.get(SIGNATURE_HEADER)
        if not signature:
            self._reply(401, {"status": "missing_signature"})
            return
        if not verify(self.server.secret, body, signature):
            self._reply(401, {"status": "invalid_signature"})
            return

        try:
            event = parse_event(body)
        except InvalidEvent as exc:
            self._reply(400, {"status": "invalid_payload", "error": str(exc)})
            return

        outcome = self.server.bridge.handle(event)
        # 202: accepted but set aside in the DLQ; the sender does not need to resend it.
        code = 202 if outcome.status == "dead_lettered" else 200
        self._reply(code, outcome.to_dict())

    def _reply(self, code: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.server.quiet:
            super().log_message(format, *args)


@contextmanager
def serve_in_background(server: WebhookServer) -> Iterator[WebhookServer]:
    """Run serve_forever() in a thread; on exit, stop it and close the listening socket."""
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
