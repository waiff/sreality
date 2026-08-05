"""Resend delivery webhook (Wave 3).

Resend posts email delivery events (delivered / bounced / complained) signed with
Svix. This route verifies the Svix HMAC over the RAW body with the stdlib (no
dependency — the same auth class as the Stripe webhook in api/routes/billing.py),
dedups by the svix-id, advances the matching channel_sends row's status by
provider_message_id, and on a bounce/complaint inserts a GLOBAL suppression so that
address is never emailed again (migration 367). Ships DARK: 503 without
RESEND_WEBHOOK_SECRET.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api import dependencies as deps

LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_REPLAY_TOLERANCE_S = 300


def verify_svix_signature(
    payload: bytes,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    secret: str,
    *,
    now: float | None = None,
) -> bool:
    """Constant-time Svix HMAC check over `{id}.{timestamp}.{body}` (the Resend
    webhook scheme). `secret` is the `whsec_<base64>` signing secret."""
    if not (svix_id and svix_timestamp and svix_signature):
        return False
    try:
        ts = int(svix_timestamp)
    except (TypeError, ValueError):
        return False
    if abs((time.time() if now is None else now) - ts) > _REPLAY_TOLERANCE_S:
        return False
    raw_secret = secret[6:] if secret.startswith("whsec_") else secret
    try:
        key = base64.b64decode(raw_secret)
    except Exception:  # noqa: BLE001 — a malformed secret authenticates no one
        return False
    signed = f"{svix_id}.{svix_timestamp}.".encode() + payload
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()
    # svix-signature is space-separated "v1,<b64sig>" tokens (supports rotation).
    for token in svix_signature.split():
        _, _, sig = token.partition(",")
        if sig and hmac.compare_digest(expected, sig):
            return True
    return False


_STATUS_BY_TYPE = {
    "email.delivered": "delivered",
    "email.bounced": "bounced",
    "email.complained": "complained",
}
_SUPPRESS_TYPES = {"email.bounced": "bounce", "email.complained": "complaint"}


def _recipient(data: dict[str, Any]) -> str | None:
    to = data.get("to")
    if isinstance(to, list):
        return to[0] if to else None
    return to if isinstance(to, str) else None


@router.post("/resend")
async def resend_webhook(
    request: Request,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    secret = os.environ.get("RESEND_WEBHOOK_SECRET")
    if not secret:
        # Fail closed: without the signing secret nothing can be authenticated.
        raise HTTPException(status_code=503, detail="Resend webhook is not configured")
    payload = await request.body()
    if not verify_svix_signature(
        payload,
        request.headers.get("svix-id"),
        request.headers.get("svix-timestamp"),
        request.headers.get("svix-signature"),
        secret,
    ):
        raise HTTPException(status_code=400, detail="Invalid Svix signature")
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event_id = request.headers.get("svix-id")
    etype = event.get("type")
    if not isinstance(event_id, str) or not isinstance(etype, str):
        raise HTTPException(status_code=400, detail="Malformed Resend event")

    # ONE transaction: the idempotency INSERT and the status/suppression writes
    # commit or roll back together, so a mid-handler crash lets Svix's retry
    # reprocess rather than short-circuit as a duplicate for work never done.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO resend_webhook_events (event_id, type, payload) "
            "VALUES (%s, %s, %s::jsonb) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event_id, etype, json.dumps(event)),
        )
        if cur.fetchone() is None:
            return {"received": True, "duplicate": True}

        new_status = _STATUS_BY_TYPE.get(etype)
        if new_status is None:
            return {"received": True, "ignored": etype}

        data = event.get("data") or {}
        email_id = data.get("email_id") or data.get("id")
        if isinstance(email_id, str) and email_id:
            # Advance the send row's status; never downgrade a row already marked
            # bounced/complained back to delivered on an out-of-order event.
            cur.execute(
                "UPDATE channel_sends SET status = %s "
                "WHERE provider_message_id = %s AND channel = 'email' "
                "  AND status NOT IN ('bounced', 'complained')",
                (new_status, email_id),
            )

        source = _SUPPRESS_TYPES.get(etype)
        recipient = _recipient(data)
        if source and recipient:
            cur.execute(
                "INSERT INTO notification_suppression "
                "  (channel, address, reason, source) "
                "VALUES ('email', %s, %s, %s) "
                "ON CONFLICT (channel, address) DO NOTHING",
                (recipient, f"resend {etype}", source),
            )
        return {"received": True, "type": etype, "status": new_status}
