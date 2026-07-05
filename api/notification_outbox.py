"""The notification delivery outbox (Sprint N PR 3).

A source-agnostic drainer: it reads `notification_dispatches` (both watchdog and
collection_monitor producers, migration 206/211) and, for every external channel
the producer stamped into `target_channels`, delivers one message through the
audited `ChannelClient` into the `channel_sends` ledger. in-app needs nothing —
the feed reads the event row directly.

Gated dark: the loop only starts when a transport is configured (see
api/main.py), and a channel with no configured transport OR no operator
recipient is skipped (no `failed` rows pile up). So nothing sends until the
operator provisions Resend / Telegram AND opts a watchdog/collection into a
channel.

Two passes per drain:
  - NEW:   un-sent (event, channel) pairs (LEFT JOIN channel_sends on the
           deterministic `notif:{dispatch}:{channel}` dedupe_key) → claim + send.
  - RETRY: `failed` rows whose `next_attempt_at` is due and `attempts < max` →
           re-attempt (linear backoff lives in ChannelClient._finalize).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

from api.transports.base import RenderedMessage
from scraper import db as scraper_db

if TYPE_CHECKING:
    import psycopg
    from api.channel_client import ChannelClient

LOG = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_WINDOW_DAYS = 7  # don't deliver alerts older than this (a long outage backlog)
_RECIPIENT_KEYS = {
    "email": "notification_email_to",
    "telegram": "notification_telegram_chat_id",
}

_SUBJECTS = {
    "new": "Nový inzerát",
    "price_drop": "Zlevněno",
    "price_rise": "Zdraženo",
    "inactive": "Staženo z nabídky",
    "reactivated": "Znovu v nabídce",
    "new_source": "Nový zdroj nabídky",
    "broker_change": "Změna makléře",
}


def _spa_base() -> str:
    return os.environ.get("SPA_BASE_URL", "").rstrip("/")


def _fmt_price(czk: int | None, unit: str | None) -> str:
    if czk is None:
        return "cena neuvedena"
    return f"{czk:,}".replace(",", " ") + " Kč" + (f"/{unit}" if unit else "")


def compose_message(row: dict[str, Any]) -> RenderedMessage:
    """Build a channel-agnostic message from a dispatch + its listing fields.

    Pure read over already-joined columns; each transport renders the slice it
    needs (email → subject + text; telegram → text + deep_link)."""
    if row.get("source_kind") == "system_health":
        # A system alert is about the pipeline, not a listing — deliver its stored
        # message verbatim (no listing join fields; sreality_id is NULL).
        text = row.get("message") or "Systémové upozornění"
        return RenderedMessage(
            subject="Systémové upozornění",
            body_text=text,
            deep_link=f"{_spa_base()}/notifications" if _spa_base() else "",
        )
    kind = row.get("change_kind") or "new"
    disposition = row.get("disposition") or ""
    locality = row.get("locality") or f"id {row.get('sreality_id')}"
    label = _SUBJECTS.get(kind, "Změna inzerátu")
    where = " ".join(p for p in (disposition, locality) if p).strip()
    subject = f"{label}: {where}" if where else label

    lines = [subject]
    price = _fmt_price(row.get("price_czk"), row.get("price_unit"))
    if kind in ("price_drop", "price_rise") and row.get("prev_price_czk"):
        lines.append(
            f"{_fmt_price(row.get('prev_price_czk'), row.get('price_unit'))}"
            f" → {_fmt_price(row.get('trigger_price_czk') or row.get('price_czk'), row.get('price_unit'))}"
        )
    else:
        lines.append(price)

    sid = row.get("sreality_id")
    deep_link = f"{_spa_base()}/listing/{sid}" if sid is not None else _spa_base()
    return RenderedMessage(
        subject=subject,
        body_text="\n".join(lines),
        deep_link=deep_link,
    )


def _resolve_recipient(conn: "psycopg.Connection", channel: str) -> str | None:
    key = _RECIPIENT_KEYS.get(channel)
    if not key:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    val = row[0] if row else None
    return val.strip() if isinstance(val, str) and val.strip() else None


_NEW_COLS = (
    "d.id::text, d.source_kind, d.change_kind, d.sreality_id, "
    "d.subscription_id::text, d.collection_id, "
    "d.trigger_price_czk, d.prev_price_czk, "
    "l.locality, l.disposition, l.price_czk, l.price_unit, l.category_main, d.message, ch"
)

_RETRY_COLS = (
    "cs.id, cs.channel, cs.recipient, cs.consumer, "
    "d.source_kind, d.change_kind, d.sreality_id, "
    "d.trigger_price_czk, d.prev_price_czk, "
    "l.locality, l.disposition, l.price_czk, l.price_unit, l.category_main, d.message"
)


def drain_once(
    conn: "psycopg.Connection",
    channel_client: "ChannelClient",
    *,
    limit: int = 200,
    max_attempts: int = _MAX_ATTEMPTS,
) -> dict[str, int]:
    """One delivery pass. Returns counters."""
    configured = sorted(channel_client.configured_channels())
    if not configured:
        return {"sent": 0, "failed": 0, "skipped": 0, "retried": 0}

    recipients = {ch: _resolve_recipient(conn, ch) for ch in configured}
    sent = failed = skipped = retried = 0

    # --- NEW pairs ---
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_NEW_COLS} "
            "FROM notification_dispatches d "
            "CROSS JOIN LATERAL unnest(d.target_channels) AS ch "
            "LEFT JOIN channel_sends cs "
            "  ON cs.dedupe_key = 'notif:' || d.id::text || ':' || ch "
            "LEFT JOIN listings l ON l.sreality_id = d.sreality_id "
            "WHERE cs.id IS NULL "
            "  AND ch = ANY(%(channels)s) "
            "  AND d.dispatched_at > now() - %(win)s::interval "
            "ORDER BY d.dispatched_at "
            "LIMIT %(limit)s",
            {"channels": configured, "win": f"{_WINDOW_DAYS} days", "limit": limit},
        )
        new_rows = cur.fetchall()

    for r in new_rows:
        (dispatch_id, source_kind, change_kind, sreality_id, subscription_id,
         collection_id, trigger_price_czk, prev_price_czk, locality, disposition,
         price_czk, price_unit, _category_main, message, ch) = r
        recipient = recipients.get(ch)
        if not recipient:
            skipped += 1
            continue
        msg = compose_message({
            "source_kind": source_kind, "message": message,
            "change_kind": change_kind, "sreality_id": sreality_id,
            "locality": locality, "disposition": disposition,
            "price_czk": price_czk, "price_unit": price_unit,
            "trigger_price_czk": trigger_price_czk, "prev_price_czk": prev_price_czk,
        })
        out = channel_client.send(
            channel=ch, recipient=recipient, message=msg,
            consumer=source_kind,
            dedupe_key=f"notif:{dispatch_id}:{ch}",
            notification_id=dispatch_id,
            source_kind=source_kind,
            source_id=subscription_id or (str(collection_id) if collection_id is not None else None),
            category="transactional",
        )
        if out.get("status") == "sent":
            sent += 1
        elif out.get("status") == "failed":
            failed += 1
        else:
            skipped += 1

    # --- RETRY failed + due ---
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_RETRY_COLS} "
            "FROM channel_sends cs "
            "JOIN notification_dispatches d ON d.id = cs.notification_id "
            "LEFT JOIN listings l ON l.sreality_id = d.sreality_id "
            "WHERE cs.status = 'failed' AND cs.attempts < %(max_attempts)s "
            "  AND cs.channel = ANY(%(channels)s) "
            "  AND (cs.next_attempt_at IS NULL OR cs.next_attempt_at <= now()) "
            "  AND cs.consumer IN ('watchdog', 'collection_monitor') "
            "ORDER BY cs.created_at "
            "LIMIT %(limit)s",
            {"max_attempts": max_attempts, "channels": configured, "limit": limit},
        )
        retry_rows = cur.fetchall()

    for r in retry_rows:
        (send_id, ch, recipient, _consumer, source_kind, change_kind, sreality_id,
         trigger_price_czk, prev_price_czk, locality, disposition,
         price_czk, price_unit, _category_main, message) = r
        if not recipient:
            continue
        msg = compose_message({
            "source_kind": source_kind, "message": message,
            "change_kind": change_kind, "sreality_id": sreality_id,
            "locality": locality, "disposition": disposition,
            "price_czk": price_czk, "price_unit": price_unit,
            "trigger_price_czk": trigger_price_czk, "prev_price_czk": prev_price_czk,
        })
        channel_client.retry(send_id=send_id, channel=ch, recipient=recipient, message=msg)
        retried += 1

    return {"sent": sent, "failed": failed, "skipped": skipped, "retried": retried}


# --- background loop -------------------------------------------------------


def _read_interval_seconds() -> int:
    conn = scraper_db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings "
                "WHERE key = 'notifications_outbox_interval_seconds'"
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return 120
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 120
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _drain_in_thread() -> dict[str, int]:
    from api.channel_client import ChannelClient
    from api.dependencies import get_transports

    conn = scraper_db.connect()
    try:
        client = ChannelClient(conn, transports=get_transports())
        return drain_once(conn, client)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


async def outbox_loop(stop_event: asyncio.Event) -> None:
    """Forever-running delivery drainer. Mirrors notifications.matcher_loop:
    own per-pass connection, app_settings-tunable interval, clean stop."""
    LOG.info("notification outbox loop starting")
    while not stop_event.is_set():
        interval = 120
        try:
            interval = await asyncio.to_thread(_read_interval_seconds)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("outbox: failed to read interval: %s", exc)

        if interval <= 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            else:
                break

        try:
            stats = await asyncio.to_thread(_drain_in_thread)
            if stats.get("sent", 0) or stats.get("failed", 0) or stats.get("retried", 0):
                LOG.info("notification outbox: %s", stats)
            else:
                LOG.debug("notification outbox: %s", stats)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("notification outbox pass failed: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            continue
        else:
            break
    LOG.info("notification outbox loop stopped")
