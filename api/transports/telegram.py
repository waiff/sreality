"""Telegram Bot API transport (Sprint N PR 4).

The mobile-native channel: free, one `requests.post` to the Bot API, native push
on a phone the operator already has — no number provisioning, no template
approval, near-zero maintenance. Recipient is the operator's `chat_id`
(`app_settings.notification_telegram_chat_id`, already resolved by the outbox).

Secret is env-only: `TELEGRAM_BOT_TOKEN`. Missing → `is_configured()` False, so
the outbox skips telegram (no send, no failed row) until provisioned.

Adding this channel was exactly what the abstraction promised: one file + one
`_build_transports` line. `channel_sends.channel` already allows 'telegram'
(migration 207), and the outbox already routes the recipient — no migration, no
matcher change.
"""

from __future__ import annotations

import os

import requests

from api.transports.base import RenderedMessage, SendResult, TransportError

_TIMEOUT_S = 15


class Telegram:
    name = "telegram"
    transport = "telegram"

    def __init__(self) -> None:
        self._token = os.environ.get("TELEGRAM_BOT_TOKEN")

    def is_configured(self) -> bool:
        return bool(self._token)

    def send(self, *, recipient: str, message: RenderedMessage) -> SendResult:
        if not self.is_configured():
            raise TransportError(
                "Telegram transport is not configured (needs TELEGRAM_BOT_TOKEN)"
            )
        # body_text already leads with the subject line; append the deep link.
        text = f"{message.body_text}\n{message.deep_link}".strip()
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": recipient, "text": text},
                timeout=_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            return SendResult(status="failed", error=f"{type(exc).__name__}: {exc}")
        if resp.status_code >= 400:
            return SendResult(
                status="failed",
                error=f"telegram HTTP {resp.status_code}: {resp.text[:300]}",
            )
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("ok") is False:
            return SendResult(status="failed", error=str(data)[:300])
        result = data.get("result") if isinstance(data, dict) else None
        msg_id = result.get("message_id") if isinstance(result, dict) else None
        return SendResult(
            status="sent",
            provider_message_id=str(msg_id) if msg_id is not None else None,
            raw=data,
        )
