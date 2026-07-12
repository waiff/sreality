"""Billing skeleton (Phase 1 increment 5): Stripe webhook + entitlement reads.

POST /billing/webhook carries its OWN auth class — the `Stripe-Signature`
HMAC over the raw request body (stdlib hmac/hashlib, no stripe SDK) — so it is
deliberately outside the bearer/JWT gates. All its writes run on the
service-role connection; `stripe_webhook_events` is the atomic idempotency
ledger (A9: INSERT .. ON CONFLICT DO NOTHING, never check-then-act) and
`entitlements.last_event_created` makes processing out-of-order tolerant
(Stripe does not guarantee delivery order).

GET /billing/me is a normal per-account read on the RLS-scoped tenant pool.
`require_entitlement(agenda)` is the plan gate future agenda routers attach
(first consumer: Wave 1) — admin/legacy callers always pass.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api import dependencies as deps
from api import tenant_pool

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# Stripe's documented replay window: reject events whose signed timestamp is
# further than this from now, so a captured request can't be replayed later.
_REPLAY_TOLERANCE_S = 300

_STATUS_PASSTHROUGH = {"active", "trialing", "past_due"}

_SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}


# --- signature verification (stdlib) ---------------------------------------

def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    """`t=<ts>,v1=<sig>[,v1=...]` -> (timestamp, all v1 signatures)."""
    timestamp: int | None = None
    v1s: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return None, []
        elif key == "v1" and value:
            v1s.append(value)
    return timestamp, v1s


def verify_stripe_signature(
    payload: bytes,
    header: str | None,
    secret: str,
    *,
    now: float | None = None,
) -> bool:
    """Constant-time check of the Stripe-Signature header over the RAW body."""
    if not header:
        return False
    timestamp, v1s = _parse_signature_header(header)
    if timestamp is None or not v1s:
        return False
    if abs((time.time() if now is None else now) - timestamp) > _REPLAY_TOLERANCE_S:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in v1s)


# --- webhook ----------------------------------------------------------------

@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        # Fail closed: without the signing secret nothing can be authenticated.
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured")
    payload = await request.body()
    if not verify_stripe_signature(
        payload, request.headers.get("Stripe-Signature"), secret
    ):
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    event_id = event.get("id")
    etype = event.get("type")
    created = event.get("created")
    if (
        not isinstance(event_id, str)
        or not isinstance(etype, str)
        or not isinstance(created, int)
    ):
        raise HTTPException(status_code=400, detail="Malformed Stripe event")

    # ONE transaction: the idempotency INSERT and the handler's writes commit or
    # roll back together — a mid-handler crash must let Stripe's retry reprocess
    # rather than hit an already-committed event row and short-circuit as a
    # duplicate for work that never happened.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stripe_webhook_events (event_id, type, created, payload) "
            "VALUES (%s, %s, %s, %s::jsonb) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event_id, etype, created, json.dumps(event)),
        )
        if cur.fetchone() is None:
            return {"received": True, "duplicate": True}
        obj = (event.get("data") or {}).get("object") or {}
        if etype == "checkout.session.completed":
            return _handle_checkout_completed(cur, obj)
        if etype in _SUBSCRIPTION_EVENTS:
            return _handle_subscription_event(cur, etype, created, obj)
        return {"received": True, "ignored": etype}


def _handle_checkout_completed(cur: Any, obj: dict[str, Any]) -> dict[str, Any]:
    try:
        account_id = str(uuid.UUID(str(obj.get("client_reference_id"))))
    except (ValueError, TypeError):
        LOG.warning(
            "checkout.session.completed without a valid client_reference_id: %r",
            obj.get("client_reference_id"),
        )
        return {"received": True, "ignored": "checkout.session.completed"}
    customer = obj.get("customer")
    if not isinstance(customer, str) or not customer:
        LOG.warning("checkout.session.completed without a customer id")
        return {"received": True, "ignored": "checkout.session.completed"}
    # Never overwrite a DIFFERENT customer id — that would silently re-point an
    # account's billing identity; only stamp a NULL or confirm the same value.
    cur.execute(
        "UPDATE accounts SET stripe_customer_id = %s WHERE id = %s "
        "AND (stripe_customer_id IS NULL OR stripe_customer_id = %s)",
        (customer, account_id, customer),
    )
    if cur.rowcount == 0:
        LOG.warning(
            "checkout.session.completed: account %s missing or already bound "
            "to another Stripe customer; ignoring",
            account_id,
        )
        return {"received": True, "ignored": "checkout.session.completed"}
    subscription = obj.get("subscription")
    if subscription:
        sub_id = (
            subscription.get("id")
            if isinstance(subscription, dict)
            else str(subscription)
        )
        cur.execute(
            "SELECT plan FROM entitlements WHERE account_id = %s", (account_id,)
        )
        row = cur.fetchone()
        plan = row[0] if row is not None else _default_plan_key(cur)
        # Minimal upsert — the authoritative plan/status/period arrive on the
        # customer.subscription.* events; this just anchors the ids.
        cur.execute(
            "INSERT INTO entitlements "
            "  (account_id, plan, status, stripe_customer_id, stripe_subscription_id) "
            "VALUES (%s, %s, 'active', %s, %s) "
            "ON CONFLICT (account_id) DO UPDATE SET "
            "  stripe_customer_id = excluded.stripe_customer_id, "
            "  stripe_subscription_id = excluded.stripe_subscription_id, "
            "  updated_at = now()",
            (account_id, plan, customer, sub_id),
        )
    return {"received": True}


def _handle_subscription_event(
    cur: Any, etype: str, created: int, sub: dict[str, Any]
) -> dict[str, Any]:
    customer = sub.get("customer")
    cur.execute("SELECT id FROM accounts WHERE stripe_customer_id = %s", (customer,))
    row = cur.fetchone()
    if row is None:
        LOG.warning("%s for unknown Stripe customer %r; ignoring", etype, customer)
        return {"received": True, "ignored": etype}
    account_id = row[0]
    cur.execute(
        "SELECT plan, last_event_created FROM entitlements WHERE account_id = %s",
        (account_id,),
    )
    existing = cur.fetchone()
    if existing is not None and existing[1] is not None and existing[1] >= created:
        return {"received": True, "stale": True}
    plan = _resolve_plan(cur, sub, existing[0] if existing is not None else None)
    status = _map_status(etype, sub.get("status"))
    period_end = sub.get("current_period_end")
    if not isinstance(period_end, (int, float)) or isinstance(period_end, bool):
        period_end = None
    cur.execute(
        "INSERT INTO entitlements "
        "  (account_id, plan, status, stripe_customer_id, stripe_subscription_id, "
        "   current_period_end, last_event_created) "
        "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s) "
        "ON CONFLICT (account_id) DO UPDATE SET "
        "  plan = excluded.plan, status = excluded.status, "
        "  stripe_customer_id = excluded.stripe_customer_id, "
        "  stripe_subscription_id = excluded.stripe_subscription_id, "
        "  current_period_end = excluded.current_period_end, "
        "  last_event_created = excluded.last_event_created, "
        "  updated_at = now()",
        (account_id, plan, status, customer, sub.get("id"), period_end, created),
    )
    return {"received": True}


def _resolve_plan(cur: Any, sub: dict[str, Any], current_plan: str | None) -> str:
    """price.lookup_key, then price.metadata.plan — first one naming an existing
    plans.key wins; otherwise keep the row's plan (default plan for a new row)."""
    items = (sub.get("items") or {}).get("data") or []
    price = (items[0].get("price") or {}) if items else {}
    for candidate in (price.get("lookup_key"), (price.get("metadata") or {}).get("plan")):
        if candidate:
            cur.execute("SELECT key FROM plans WHERE key = %s", (candidate,))
            if cur.fetchone() is not None:
                return str(candidate)
    fallback = current_plan or _default_plan_key(cur)
    LOG.warning(
        "stripe subscription price maps to no known plan "
        "(lookup_key=%r, metadata.plan=%r); keeping %r",
        price.get("lookup_key"), (price.get("metadata") or {}).get("plan"), fallback,
    )
    return fallback


def _map_status(etype: str, sub_status: Any) -> str:
    if etype == "customer.subscription.deleted":
        return "canceled"
    return sub_status if sub_status in _STATUS_PASSTHROUGH else "canceled"


def _default_plan_key(cur: Any) -> str:
    cur.execute("SELECT key FROM plans WHERE is_default LIMIT 1")
    row = cur.fetchone()
    return row[0] if row is not None else "free"


# --- entitlement reads -------------------------------------------------------

def _entitlement_view(conn: Any, account_id: Any) -> dict[str, Any]:
    """The caller's effective entitlement: their explicit row, else the default plan."""
    if account_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT e.plan, e.status, e.current_period_end, p.agendas "
                "FROM entitlements e JOIN plans p ON p.key = e.plan "
                "WHERE e.account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()
        if row is not None:
            return {
                "plan": row[0],
                "status": row[1],
                "agendas": row[3] or {},
                "current_period_end": (
                    row[2].isoformat() if hasattr(row[2], "isoformat") else row[2]
                ),
            }
    with conn.cursor() as cur:
        cur.execute("SELECT key, agendas FROM plans WHERE is_default LIMIT 1")
        row = cur.fetchone()
    if row is None:
        # Unseeded plans table: empty agendas = the gate fails closed.
        return {"plan": "free", "status": "active", "agendas": {},
                "current_period_end": None}
    return {"plan": row[0], "status": "active", "agendas": row[1] or {},
            "current_period_end": None}


@router.get("/me")
def get_billing_me(
    claims: dict = Depends(deps.verify_jwt),
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, Any]:
    """The caller's plan + agenda visibility (legacy/no-account -> default plan)."""
    account_id = tenant_pool.resolve_account_id(conn, claims)
    return _entitlement_view(conn, account_id)


def require_entitlement(agenda: str) -> Callable[..., dict]:
    """Dependency factory: 403 unless the caller's plan turns `agenda` on.

    Admin + legacy claims always pass (the operator is never billing-gated).
    Not wired to any route yet — Wave 1 attaches it per-agenda router.
    """

    def _gate(
        claims: dict = Depends(deps.verify_jwt),
        conn: Any = Depends(deps.get_db_conn),
    ) -> dict:
        meta = claims.get("app_metadata") or {}
        if (
            claims.get("legacy")
            or claims.get("is_admin") is True
            or meta.get("is_admin") is True
        ):
            return claims
        account_id = tenant_pool.resolve_account_id(conn, claims)
        ent = _entitlement_view(conn, account_id)
        if ent["status"] == "canceled" or ent["agendas"].get(agenda) is not True:
            raise HTTPException(
                status_code=403,
                detail=f"Your plan does not include {agenda!r}",
            )
        return claims

    return _gate
