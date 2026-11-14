"""Minimal urllib client that posts to the local receiver (127.0.0.1 only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .server import HOST, WEBHOOK_PATH
from .signature import SIGNATURE_HEADER

# An empty ProxyHandler ignores HTTP(S)_PROXY settings, so requests never leave this machine.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _decode(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"raw": raw.decode("utf-8", "replace")}
    return value if isinstance(value, dict) else {"raw": value}


def post_webhook(
    port: int,
    body: bytes,
    signature: Optional[str],
    *,
    path: str = WEBHOOK_PATH,
    timeout: float = 10.0,
) -> Tuple[int, Dict[str, Any]]:
    """POST body to http://127.0.0.1:<port><path>. Returns (HTTP status, JSON body)."""
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    request = urllib.request.Request(
        f"http://{HOST}:{port}{path}", data=body, headers=headers, method="POST"
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _decode(exc.read())
        finally:
            exc.close()
