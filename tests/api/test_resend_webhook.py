"""Hermetic tests for the Resend (Svix) webhook signature verification.

The HMAC check is the security boundary — it gates every status/suppression write.
These construct real Svix signatures with a known secret and assert accept/reject.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from api.routes.resend_webhook import verify_svix_signature


def _sign(secret_b64: str, svix_id: str, ts: int, body: bytes) -> str:
    key = base64.b64decode(secret_b64)
    signed = f"{svix_id}.{ts}.".encode() + body
    return "v1," + base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()


def test_verify_svix_signature_accepts_valid() -> None:
    secret_b64 = base64.b64encode(b"super-secret-key-material").decode()
    secret = "whsec_" + secret_b64
    body = b'{"type":"email.delivered","data":{"email_id":"abc"}}'
    svix_id, ts = "msg_1", int(time.time())
    sig = _sign(secret_b64, svix_id, ts, body)
    assert verify_svix_signature(body, svix_id, str(ts), sig, secret, now=ts) is True


def test_verify_svix_signature_rejects_tampered_body() -> None:
    secret_b64 = base64.b64encode(b"k").decode()
    secret = "whsec_" + secret_b64
    ts = int(time.time())
    sig = _sign(secret_b64, "m", ts, b"original")
    assert verify_svix_signature(b"tampered", "m", str(ts), sig, secret, now=ts) is False


def test_verify_svix_signature_rejects_stale_timestamp() -> None:
    secret_b64 = base64.b64encode(b"k").decode()
    secret = "whsec_" + secret_b64
    ts = int(time.time())
    sig = _sign(secret_b64, "m", ts, b"body")
    # 10-minute skew exceeds the 5-minute replay tolerance.
    assert verify_svix_signature(b"body", "m", str(ts), sig, secret, now=ts + 600) is False


def test_verify_svix_signature_rejects_wrong_secret() -> None:
    ts = int(time.time())
    sig = _sign(base64.b64encode(b"right").decode(), "m", ts, b"body")
    wrong = "whsec_" + base64.b64encode(b"wrong").decode()
    assert verify_svix_signature(b"body", "m", str(ts), sig, wrong, now=ts) is False


def test_verify_svix_signature_rejects_missing_headers() -> None:
    secret = "whsec_" + base64.b64encode(b"k").decode()
    assert verify_svix_signature(b"body", None, None, None, secret) is False
