"""Resend email transport (Sprint N PR 2).

Resend (https://resend.com) serves the TRANSACTIONAL / self-notification email
stream: a single api-key JSON POST, no SDK (`requests`, the house HTTP client),
permanent free tier far beyond a personal alert feed. Scope is transactional
only — Resend's AUP forbids cold/unsolicited outreach and stores account data in
the US, so the deferred broker-outreach (commercial) stream gets a separate
EU-hosted vendor behind this same `ChannelTransport` Protocol (see
docs/design/notification-channels.md §6/§9).

Secrets are env-only (Railway): `RESEND_API_KEY`, `EMAIL_FROM`. Missing keys
fail at `send()` (the providers' posture); `is_configured()` lets the outbox
skip the channel rather than crash.
"""

from __future__ import annotations

import os

import requests

from api.transports.base import RenderedMessage, SendResult, TransportError
from api.unsubscribe import make_unsub_token

_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT_S = 15


def _list_unsubscribe_headers(recipient: str) -> dict[str, str]:
    """RFC 8058 one-click unsubscribe headers, when configured. Empty (no headers)
    if NOTIFICATION_UNSUB_SECRET or API_PUBLIC_URL is unset — the email still sends,
    just without one-click unsubscribe (graceful, keeps the transport dark-safe)."""
    base = os.environ.get("API_PUBLIC_URL", "").rstrip("/")
    token = make_unsub_token("email", recipient)
    if not base or not token:
        return {}
    url = f"{base}/u/{token}"
    return {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


class ResendEmail:
    name = "email"
    transport = "resend"

    def __init__(self) -> None:
        self._api_key = os.environ.get("RESEND_API_KEY")
        self._from = os.environ.get("EMAIL_FROM")

    def is_configured(self) -> bool:
        return bool(self._api_key and self._from)

    def send(self, *, recipient: str, message: RenderedMessage) -> SendResult:
        if not self.is_configured():
            raise TransportError(
                "Resend email transport is not configured "
                "(needs RESEND_API_KEY + EMAIL_FROM)"
            )
        payload: dict[str, object] = {
            "from": self._from,
            "to": [recipient],
            "subject": message.subject or "Nová shoda hlídače",
            "text": f"{message.body_text}\n\n{message.deep_link}",
        }
        if message.body_html:
            payload["html"] = message.body_html
        unsub = _list_unsubscribe_headers(recipient)
        if unsub:
            payload["headers"] = unsub
        try:
            resp = requests.post(
                _ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            return SendResult(status="failed", error=f"{type(exc).__name__}: {exc}")
        if resp.status_code >= 400:
            return SendResult(
                status="failed",
                error=f"resend HTTP {resp.status_code}: {resp.text[:300]}",
            )
        data = resp.json() if resp.content else {}
        msg_id = data.get("id") if isinstance(data, dict) else None
        # Free tier → no per-message price; cost_usd stays None (unknown), per the
        # channel_sends NULL-vs-0 convention.
        return SendResult(
            status="sent",
            provider_message_id=str(msg_id) if msg_id else None,
            raw=data,
        )
