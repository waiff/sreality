"""Audited orchestrator for notification delivery — the `LLMClient` of channels.

`ChannelClient.send(...)`:
  1. CLAIM the (event, channel) pair: INSERT a `queued` channel_sends row with the
     deterministic `dedupe_key`, ON CONFLICT DO NOTHING RETURNING id. No row back =>
     already claimed/sent => idempotent no-op (restart-safe, double-send-proof, no
     advisory locks).
  2. Resolve the transport for the channel (raises TransportError listing the
     configured channels on a miss — the LLMClient.provider() mirror).
  3. transport.send(...), timed; capture provider_message_id / cost / error.
  4. UPDATE the row to its terminal status (sent | failed) with provenance.

Every send is one audited row, exactly like `llm_calls`, so delivery rate,
match->sent latency, per-channel/per-source failure, and per-day spend are all
queryable. The outbox loop (Sprint N PR 2) calls this once per (notification x
target channel) it derives; retry of a `failed` row is the outbox's job, not a
re-claim (the dedupe_key already exists).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from api.transports.base import ChannelTransport, RenderedMessage, TransportError

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)

Consumer = Literal["watchdog", "collection_monitor", "outreach"]


class ChannelClient:
    def __init__(
        self,
        conn: "psycopg.Connection",
        transports: dict[str, ChannelTransport] | None = None,
    ) -> None:
        self._conn = conn
        self._transports: dict[str, ChannelTransport] = transports or {}

    def transport(self, channel: str) -> ChannelTransport:
        try:
            return self._transports[channel]
        except KeyError as exc:
            raise TransportError(
                f"channel {channel!r} is not configured; "
                f"available: {sorted(self._transports)}"
            ) from exc

    def configured_channels(self) -> set[str]:
        """Channels whose transport has its secrets set — the outbox skips the
        rest rather than piling up `failed` rows for an unprovisioned channel."""
        return {
            name for name, t in self._transports.items() if t.is_configured()
        }

    def send(
        self,
        *,
        channel: str,
        recipient: str,
        message: RenderedMessage,
        consumer: Consumer,
        dedupe_key: str,
        notification_id: str | None = None,
        outreach_message_id: int | None = None,
        source_kind: str | None = None,
        source_id: str | None = None,
        category: str = "transactional",
    ) -> dict[str, Any]:
        """Claim, deliver, and audit one (event, channel) send. Idempotent on
        `dedupe_key`. Never raises on a transport failure — the `failed` row is
        the audit trail (mirrors run_pending_estimation / llm_calls discipline);
        only an unconfigured channel raises, and is recorded first."""
        claim_id = self._claim(
            consumer=consumer,
            channel=channel,
            recipient=recipient,
            dedupe_key=dedupe_key,
            notification_id=notification_id,
            outreach_message_id=outreach_message_id,
            source_kind=source_kind,
            source_id=source_id,
            category=category,
        )
        if claim_id is None:
            return {"status": "already_claimed", "id": None}

        try:
            transport = self.transport(channel)
        except TransportError as exc:
            self._finalize(claim_id, status="failed", error=str(exc))
            raise

        mono = time.monotonic()
        try:
            result = transport.send(recipient=recipient, message=message)
        except Exception as exc:  # noqa: BLE001 — the failed row IS the audit trail
            duration_ms = int((time.monotonic() - mono) * 1000)
            LOG.warning(
                "channel send failed id=%s channel=%s: %s", claim_id, channel, exc
            )
            self._finalize(
                claim_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}"[:1000],
                duration_ms=duration_ms,
            )
            return {"status": "failed", "id": claim_id, "error": str(exc)}

        duration_ms = int((time.monotonic() - mono) * 1000)
        self._finalize(
            claim_id,
            status=result.status,
            error=result.error,
            provider_message_id=result.provider_message_id,
            transport=transport.transport,
            cost_usd=result.cost_usd,
            duration_ms=duration_ms,
        )
        return {
            "status": result.status,
            "id": claim_id,
            "provider_message_id": result.provider_message_id,
        }

    def _claim(
        self,
        *,
        consumer: str,
        channel: str,
        recipient: str,
        dedupe_key: str,
        notification_id: str | None,
        outreach_message_id: int | None,
        source_kind: str | None,
        source_id: str | None,
        category: str,
    ) -> int | None:
        """INSERT a queued row, or None if this (event, channel) is already claimed."""
        sql = (
            "INSERT INTO channel_sends "
            "  (consumer, notification_id, outreach_message_id, source_kind, source_id, "
            "   channel, recipient, category, status, dedupe_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s) "
            "ON CONFLICT (dedupe_key) DO NOTHING "
            "RETURNING id"
        )
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    consumer,
                    notification_id,
                    outreach_message_id,
                    source_kind,
                    source_id,
                    channel,
                    recipient,
                    category,
                    dedupe_key,
                ),
            )
            row = cur.fetchone()
        return int(row[0]) if row else None

    def _finalize(
        self,
        claim_id: int,
        *,
        status: str,
        error: str | None = None,
        provider_message_id: str | None = None,
        transport: str | None = None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
    ) -> None:
        # On failure schedule a retry with linear backoff (5 min × the new
        # attempt count); on success clear the cursor. The outbox retry pass
        # picks up failed rows whose next_attempt_at is due (and attempts < max).
        sql = (
            "UPDATE channel_sends SET "
            "  status = %s, error_message = %s, provider_message_id = %s, "
            "  transport = COALESCE(%s, transport), cost_usd = %s, duration_ms = %s, "
            "  attempts = attempts + 1, "
            "  sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END, "
            "  next_attempt_at = CASE WHEN %s = 'failed' "
            "    THEN now() + ((attempts + 1) * interval '5 minutes') ELSE NULL END "
            "WHERE id = %s"
        )
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    status,
                    error,
                    provider_message_id,
                    transport,
                    cost_usd,
                    duration_ms,
                    status,
                    status,
                    claim_id,
                ),
            )

    def retry(
        self,
        *,
        send_id: int,
        channel: str,
        recipient: str,
        message: RenderedMessage,
    ) -> dict[str, Any]:
        """Re-attempt an existing `failed` channel_sends row (the outbox retry
        pass). No claim — the row exists; re-run the transport and re-finalize
        (attempts + next_attempt_at advance). Never raises on a send failure."""
        transport = self.transport(channel)
        mono = time.monotonic()
        try:
            result = transport.send(recipient=recipient, message=message)
        except Exception as exc:  # noqa: BLE001 — the failed row IS the audit trail
            self._finalize(
                send_id, status="failed",
                error=f"{type(exc).__name__}: {exc}"[:1000],
                duration_ms=int((time.monotonic() - mono) * 1000),
            )
            return {"status": "failed", "id": send_id, "error": str(exc)}
        self._finalize(
            send_id,
            status=result.status,
            error=result.error,
            provider_message_id=result.provider_message_id,
            transport=transport.transport,
            cost_usd=result.cost_usd,
            duration_ms=int((time.monotonic() - mono) * 1000),
        )
        return {"status": result.status, "id": send_id}
