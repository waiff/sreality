"""One-click unsubscribe tokens (RFC 8058) — stdlib HMAC, no dependency.

A token encodes `(channel, address)` and is signed with `NOTIFICATION_UNSUB_SECRET`.
`POST /u/{token}` (api/routes/unsubscribe.py) verifies it and inserts a global
`notification_suppression` row. DARK without the secret: `make_unsub_token` returns
None, so the email transport emits no `List-Unsubscribe` header and the endpoint
can authenticate no one. Same auth class as the Stripe/Resend webhooks — the HMAC
IS the authentication (no session, works for logged-out recipients).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os


def _secret() -> str | None:
    return os.environ.get("NOTIFICATION_UNSUB_SECRET") or None


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_unsub_token(channel: str, address: str) -> str | None:
    """Sign `channel:address`. None when unconfigured (→ no List-Unsubscribe header)."""
    secret = _secret()
    if not secret:
        return None
    payload = f"{channel}:{address}".encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_unsub_token(token: str) -> tuple[str, str] | None:
    """Return `(channel, address)` for a valid token, else None (bad sig, malformed,
    or unconfigured). channel is colon-free ('email'/'telegram'), so the first ':'
    splits it from the address (email addresses carry no colon)."""
    secret = _secret()
    if not secret or not token or "." not in token:
        return None
    payload_b64, _, sig_b64 = token.partition(".")
    try:
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
    except Exception:  # noqa: BLE001 — a malformed token authenticates no one
        return None
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        channel, _, address = payload.decode().partition(":")
    except Exception:  # noqa: BLE001
        return None
    if not channel or not address:
        return None
    return channel, address
