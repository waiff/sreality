"""Neutral types + Protocol every channel transport implements.

Mirrors `api/providers/base.py` (the LLM-provider abstraction): a Protocol +
frozen dataclasses, so adding a delivery channel (email / Telegram / push / …)
is one new file implementing `ChannelTransport`, registered in
`api/dependencies._build_transports`. The audited orchestrator that drives a
transport and writes the `channel_sends` ledger is `api/channel_client.py` (the
`LLMClient` analog). See docs/design/notification-channels.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class RenderedMessage:
    """One notification rendered to channel-agnostic parts.

    A transport uses the slice it needs: email uses subject + html (falling back
    to body_text); Telegram uses body_text + deep_link. Composed once per event
    (`compose_*` in api/notifications.py, Sprint C/N), never per transport.
    """
    body_text: str
    deep_link: str
    subject: str | None = None
    body_html: str | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class SendResult:
    """One transport's outcome for one send. `status` is terminal here
    ('sent' | 'failed'); the ledger's 'queued' is the pre-send state."""
    status: str
    provider_message_id: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    raw: Any = field(repr=False, default=None)


class TransportError(Exception):
    """Raised when a channel is not configured, or a transport fails fatally."""


class ChannelTransport(Protocol):
    """One delivery backend (Resend email, Telegram, …).

    Implementations read their own secret from env in `__init__` and store it,
    but raise only inside `send()` — a missing key fails the request, not boot
    (the providers' posture). `is_configured()` lets the outbox skip an
    unconfigured channel rather than crash (the image_storage precedent).
    """

    name: str       # the channel: 'email' | 'telegram'
    transport: str  # the concrete vendor: 'resend' | 'telegram'

    def is_configured(self) -> bool: ...

    def send(self, *, recipient: str, message: RenderedMessage) -> SendResult: ...
