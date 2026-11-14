"""HMAC-SHA256 signatures over the raw request body."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional, Tuple

SIGNATURE_HEADER = "X-Webhook-Signature"
SIGNATURE_PREFIX = "sha256="

SECRET_ENV = "WEBHOOK_SECRET"
# Fictitious value for the local demo only. Set WEBHOOK_SECRET to override it.
DEMO_SECRET = "demo-secret-not-for-production"


def load_secret() -> Tuple[str, bool]:
    """Return (secret, taken_from_env). Falls back to DEMO_SECRET when the env var is unset or empty."""
    value = os.environ.get(SECRET_ENV, "")
    if value:
        return value, True
    return DEMO_SECRET, False


def sign(secret: str, body: bytes) -> str:
    """Return the header value: "sha256=" + lowercase hex HMAC-SHA256 of the raw body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify(secret: str, body: bytes, header_value: Optional[str]) -> bool:
    """Check a signature header against the raw body using a constant-time comparison."""
    if not header_value:
        return False
    expected = sign(secret, body).encode("ascii")
    received = header_value.strip().encode("utf-8", "replace")
    return hmac.compare_digest(expected, received)
