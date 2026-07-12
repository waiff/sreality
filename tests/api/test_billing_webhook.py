"""Tests for /billing/* — the Stripe webhook + the require_entitlement gate.

Hermetic (test_pipeline conventions): get_db_conn is overridden with a scripted
fake connection; signatures are built in-test with the same stdlib hmac math the
route verifies with, so a passing test proves the two sides agree.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import billing


_SECRET = "whsec_test_secret"


# --- scripted fake connection (mirrors tests/api/test_pipeline.py) ----------

class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        q = " ".join(sql.split())
        self._conn.executed.append((q, params))
        for predicate, rows in self._conn.script:
            if predicate(q):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Tx:
    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Tx:
        return _Tx()

    def cursor(self) -> _Cur:
        return _Cur(self)


@pytest.fixture(autouse=True)
def _secret_and_cleanup(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _SECRET)
    yield
    api_main.app.dependency_overrides.clear()


def _client(conn: _FakeConn) -> Any:
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    return TestClient(api_main.app)


def _sign(payload: bytes, secret: str = _SECRET, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(
        secret.encode(), f"{t}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={t},v1={mac}"


def _event(
    etype: str,
    obj: dict[str, Any],
    *,
    event_id: str = "evt_1",
    created: int = 1_000,
) -> bytes:
    return json.dumps(
        {"id": event_id, "type": etype, "created": created, "data": {"object": obj}}
    ).encode()


def _post(client: Any, payload: bytes, sig: str | None) -> Any:
    headers = {"Content-Type": "application/json"}
    if sig is not None:
        headers["Stripe-Signature"] = sig
    return client.post("/billing/webhook", content=payload, headers=headers)


_EVENTS_INSERT = lambda q: "INSERT INTO stripe_webhook_events" in q  # noqa: E731


# --- signature verification --------------------------------------------------

def test_verify_signature_valid():
    payload = b'{"id": "evt_x"}'
    assert billing.verify_stripe_signature(payload, _sign(payload), _SECRET)


def test_verify_signature_accepts_any_matching_v1():
    payload = b"{}"
    t = int(time.time())
    good = _sign(payload, t=t).split("v1=")[1]
    header = f"t={t},v1=deadbeef,v1={good}"
    assert billing.verify_stripe_signature(payload, header, _SECRET)


def test_webhook_tampered_body_400():
    payload = _event("invoice.paid", {})
    sig = _sign(payload)
    res = _post(_client(_FakeConn([])), payload + b" ", sig)
    assert res.status_code == 400


def test_webhook_stale_timestamp_400():
    payload = _event("invoice.paid", {})
    sig = _sign(payload, t=int(time.time()) - 400)
    res = _post(_client(_FakeConn([])), payload, sig)
    assert res.status_code == 400


def test_webhook_missing_header_400():
    payload = _event("invoice.paid", {})
    res = _post(_client(_FakeConn([])), payload, None)
    assert res.status_code == 400


def test_webhook_unset_secret_503(monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    payload = _event("invoice.paid", {})
    res = _post(_client(_FakeConn([])), payload, _sign(payload))
    assert res.status_code == 503


# --- idempotency + event handling ---------------------------------------------

def test_duplicate_event_short_circuits():
    conn = _FakeConn([(_EVENTS_INSERT, [])])  # ON CONFLICT -> no row returned
    payload = _event(
        "customer.subscription.updated", {"customer": "cus_1", "status": "active"}
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.status_code == 200
    assert res.json() == {"received": True, "duplicate": True}
    assert len(conn.executed) == 1, "no processing beyond the idempotency INSERT"


def test_unknown_type_stored_and_ignored():
    conn = _FakeConn([(_EVENTS_INSERT, [("evt_1",)])])
    payload = _event("invoice.paid", {})
    res = _post(_client(conn), payload, _sign(payload))
    assert res.status_code == 200
    assert res.json() == {"received": True, "ignored": "invoice.paid"}
    assert any(_EVENTS_INSERT(q) for q, _ in conn.executed)


def test_subscription_updated_upserts_entitlement():
    acct = uuid.uuid4()
    conn = _FakeConn([
        (_EVENTS_INSERT, [("evt_1",)]),
        (lambda q: "SELECT id FROM accounts WHERE stripe_customer_id" in q, [(acct,)]),
        (lambda q: "SELECT plan, last_event_created FROM entitlements" in q, []),
        (lambda q: "SELECT key FROM plans WHERE key" in q, [("pro",)]),
    ])
    payload = _event(
        "customer.subscription.updated",
        {
            "id": "sub_1",
            "customer": "cus_1",
            "status": "trialing",
            "current_period_end": 1_234_567,
            "items": {"data": [{"price": {"lookup_key": "pro", "metadata": {}}}]},
        },
        created=2_000,
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.status_code == 200
    assert res.json() == {"received": True}
    upsert = next(
        (q, p) for q, p in conn.executed if "INSERT INTO entitlements" in q
    )
    assert "ON CONFLICT (account_id) DO UPDATE" in upsert[0]
    assert upsert[1] == (acct, "pro", "trialing", "cus_1", "sub_1", 1_234_567, 2_000)


def test_subscription_out_of_order_is_stale():
    acct = uuid.uuid4()
    conn = _FakeConn([
        (_EVENTS_INSERT, [("evt_1",)]),
        (lambda q: "SELECT id FROM accounts WHERE stripe_customer_id" in q, [(acct,)]),
        (
            lambda q: "SELECT plan, last_event_created FROM entitlements" in q,
            [("pro", 5_000)],
        ),
    ])
    payload = _event(
        "customer.subscription.updated",
        {"id": "sub_1", "customer": "cus_1", "status": "active"},
        created=2_000,  # older than the stored last_event_created
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.status_code == 200
    assert res.json() == {"received": True, "stale": True}
    assert not any("INSERT INTO entitlements" in q for q, _ in conn.executed)


def test_subscription_deleted_maps_canceled():
    acct = uuid.uuid4()
    conn = _FakeConn([
        (_EVENTS_INSERT, [("evt_1",)]),
        (lambda q: "SELECT id FROM accounts WHERE stripe_customer_id" in q, [(acct,)]),
        (
            lambda q: "SELECT plan, last_event_created FROM entitlements" in q,
            [("pro", 1_000)],
        ),
    ])
    payload = _event(
        "customer.subscription.deleted",
        {"id": "sub_1", "customer": "cus_1", "status": "active"},
        created=2_000,
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.json() == {"received": True}
    upsert = next(p for q, p in conn.executed if "INSERT INTO entitlements" in q)
    assert upsert[2] == "canceled"
    assert upsert[1] == "pro", "no price in payload -> the row's current plan is kept"


def test_subscription_unknown_customer_ignored():
    conn = _FakeConn([(_EVENTS_INSERT, [("evt_1",)])])
    payload = _event(
        "customer.subscription.updated",
        {"id": "sub_1", "customer": "cus_nobody", "status": "active"},
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.json() == {"received": True, "ignored": "customer.subscription.updated"}
    assert not any("INSERT INTO entitlements" in q for q, _ in conn.executed)


def test_checkout_completed_stamps_customer_and_seeds_entitlement():
    acct = str(uuid.uuid4())
    conn = _FakeConn([
        (_EVENTS_INSERT, [("evt_1",)]),
        (lambda q: "UPDATE accounts SET stripe_customer_id" in q, [(1,)]),
        (lambda q: "SELECT plan FROM entitlements" in q, []),
        (lambda q: "WHERE is_default" in q, [("free",)]),
    ])
    payload = _event(
        "checkout.session.completed",
        {"client_reference_id": acct, "customer": "cus_9", "subscription": "sub_9"},
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.json() == {"received": True}
    stamp = next(
        (q, p) for q, p in conn.executed
        if "UPDATE accounts SET stripe_customer_id" in q
    )
    # Guarded write: only a NULL (or identical) customer id may be stamped.
    assert "stripe_customer_id IS NULL OR stripe_customer_id = %s" in stamp[0]
    seed = next(p for q, p in conn.executed if "INSERT INTO entitlements" in q)
    assert seed == (acct, "free", "cus_9", "sub_9")


def test_checkout_completed_customer_mismatch_ignored():
    acct = str(uuid.uuid4())
    conn = _FakeConn([
        (_EVENTS_INSERT, [("evt_1",)]),
        (lambda q: "UPDATE accounts SET stripe_customer_id" in q, []),  # rowcount 0
    ])
    payload = _event(
        "checkout.session.completed",
        {"client_reference_id": acct, "customer": "cus_9", "subscription": "sub_9"},
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.json() == {"received": True, "ignored": "checkout.session.completed"}
    assert not any("INSERT INTO entitlements" in q for q, _ in conn.executed)


def test_checkout_completed_missing_reference_ignored():
    conn = _FakeConn([(_EVENTS_INSERT, [("evt_1",)])])
    payload = _event(
        "checkout.session.completed", {"customer": "cus_9", "subscription": "sub_9"}
    )
    res = _post(_client(conn), payload, _sign(payload))
    assert res.json() == {"received": True, "ignored": "checkout.session.completed"}
    assert not any("UPDATE accounts" in q for q, _ in conn.executed)


# --- require_entitlement -------------------------------------------------------

def test_require_entitlement_legacy_bypasses():
    gate = billing.require_entitlement("browse")
    claims = {"sub": None, "is_admin": True, "legacy": True}
    assert gate(claims=claims, conn=object()) is claims


def test_require_entitlement_admin_bypasses():
    gate = billing.require_entitlement("browse")
    claims = {"sub": str(uuid.uuid4()), "app_metadata": {"is_admin": True}}
    assert gate(claims=claims, conn=object()) is claims


def test_require_entitlement_agenda_off_403():
    acct = uuid.uuid4()
    conn = _FakeConn([
        (lambda q: "SELECT account_id FROM account_members" in q, [(acct,)]),
        (
            lambda q: "FROM entitlements e JOIN plans p" in q,
            [("pro", "active", None, {"browse": False})],
        ),
    ])
    gate = billing.require_entitlement("browse")
    with pytest.raises(fastapi.HTTPException) as ei:
        gate(claims={"sub": str(uuid.uuid4())}, conn=conn)
    assert ei.value.status_code == 403


def test_require_entitlement_canceled_403():
    acct = uuid.uuid4()
    conn = _FakeConn([
        (lambda q: "SELECT account_id FROM account_members" in q, [(acct,)]),
        (
            lambda q: "FROM entitlements e JOIN plans p" in q,
            [("pro", "canceled", None, {"browse": True})],
        ),
    ])
    gate = billing.require_entitlement("browse")
    with pytest.raises(fastapi.HTTPException) as ei:
        gate(claims={"sub": str(uuid.uuid4())}, conn=conn)
    assert ei.value.status_code == 403


def test_require_entitlement_default_plan_fallback_passes():
    conn = _FakeConn([
        (lambda q: "SELECT account_id FROM account_members" in q, []),
        (lambda q: "WHERE is_default" in q, [("free", {"browse": True})]),
    ])
    gate = billing.require_entitlement("browse")
    claims = {"sub": str(uuid.uuid4())}
    assert gate(claims=claims, conn=conn) is claims
