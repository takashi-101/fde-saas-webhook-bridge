"""Command line: demo / serve / send / dlq."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .bridge import WebhookBridge
from .client import post_webhook
from .demo import DemoDownstream, run_demo
from .server import WebhookServer
from .signature import SECRET_ENV, load_secret, sign
from .store import EventStore


def _cmd_demo(args: argparse.Namespace) -> int:
    return run_demo(args.db)


def _cmd_serve(args: argparse.Namespace) -> int:
    secret, from_env = load_secret()
    if not from_env:
        print(f"warning: {SECRET_ENV} is not set; using the built-in demo secret", file=sys.stderr)
    bridge = WebhookBridge(
        EventStore(args.db),
        DemoDownstream(),
        max_attempts=args.max_attempts,
        base_delay=args.base_delay,
    )
    server = WebhookServer(args.port, bridge, secret)
    print(f"listening on {server.url} (db: {args.db}); press Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    body = Path(args.file).read_bytes()
    secret, _ = load_secret()
    code, reply = post_webhook(args.port, body, sign(secret, body))
    print(f"HTTP {code} {json.dumps(reply, ensure_ascii=False)}")
    return 0 if 200 <= code < 300 else 1


def _cmd_dlq(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"error: {args.db} does not exist", file=sys.stderr)
        return 1
    rows = EventStore(args.db).list_dead_letters()
    print(f"{len(rows)} dead-lettered event(s) in {args.db}")
    for row in rows:
        print(
            f"  {row['event_id']}  {row['event_type']}  attempts={row['attempts']}"
            f"  last_error={row['last_error']}  at={row['dead_lettered_at']}"
        )
        if args.payload:
            print(f"    payload={row['payload']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m webhook_bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="start the server, post sample notifications to it, print a summary")
    p.add_argument("--db", default="demo.db", help="SQLite file; deleted and recreated (default: demo.db)")
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("serve", help="run the receiver on 127.0.0.1 until Ctrl+C")
    p.add_argument("--db", default="bridge.db", help="SQLite file (default: bridge.db)")
    p.add_argument("--port", type=int, default=8080, help="port on 127.0.0.1 (default: 8080)")
    p.add_argument("--max-attempts", type=int, default=3, help="handler attempts per event (default: 3)")
    p.add_argument("--base-delay", type=float, default=0.5, help="first backoff in seconds (default: 0.5)")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("send", help="sign a JSON file and POST it to the local receiver")
    p.add_argument("file", help="JSON file to send as-is")
    p.add_argument("--port", type=int, default=8080, help="port on 127.0.0.1 (default: 8080)")
    p.set_defaults(func=_cmd_send)

    p = sub.add_parser("dlq", help="list the dead-letter queue")
    p.add_argument("--db", default="bridge.db", help="SQLite file (default: bridge.db)")
    p.add_argument("--payload", action="store_true", help="also print the stored payload")
    p.set_defaults(func=_cmd_dlq)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
