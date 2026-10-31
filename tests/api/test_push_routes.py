"""The four /push routes, hermetic (notifications push W4, PROGRAM.md § 3.4): the key and its 503, every validation
422 (none repeats the endpoint, none opens the service role), each write's statements on the caller's own account,
and the never-acts 403. The statements' semantics on the real schema (the three PUT cases, the cap, DELETE and
opened) run in tests/test_notifications_push_live.py."""

from __future__ import annotations

import contextlib
import logging
import os
import uuid
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

import psycopg  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from api import dependencies as deps  # noqa: E402
from api import main as api_main  # noqa: E402
from api import push, push_devices, tenant_pool  # noqa: E402
from tests.api._script_conn import ScriptConn  # noqa: E402

_SUB = "11111111-1111-1111-1111-111111111111"
_ME = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OTHER = uuid.UUID("44444444-4444-4444-4444-444444444444")
ORIGIN = "https://app.example.cz"
ENDPOINT = "https://web.push.apple.com/QGuHfVhTMmGc4fZDuWs8fIY4ELbdpXFgx8ibAmN3UDPqx0QvGhIA9xlu7Ok"
P256DH = push.b64url(ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(*push._POINT))
AUTH = push.b64url(os.urandom(16))


def _body(**over: Any) -> dict[str, Any]:
    body = {"endpoint": ENDPOINT, "expirationTime": None, "keys": {"p256dh": P256DH, "auth": AUTH},
            "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X)"}
    body.update(over)
    return body


class _Opener:
    """deps.open_background_conn, scripted: counts how often the service role was opened."""

    def __init__(self, conn: ScriptConn) -> None:
        self.conn, self.opened = conn, 0

    def __call__(self) -> Any:
        self.opened += 1
        return contextlib.nullcontext(self.conn)


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch) -> _Opener:
    opener = _Opener(ScriptConn([(lambda q: q.startswith("INSERT INTO push_subscriptions"), [(7,)])]))
    monkeypatch.setattr(deps, "open_background_conn", opener)
    return opener


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, service: _Opener) -> Any:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", f"http://localhost:5173, {ORIGIN},chrome-extension://abc")
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: ScriptConn([])
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"sub": _SUB}
    # A distinctive account, asserted to ARRIVE (tenancy.md: never stub a resolver to its absence).
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda c, claims: _ME)
    yield TestClient(api_main.app, headers={"Origin": ORIGIN})
    api_main.app.dependency_overrides.clear()


def _sql(opener: _Opener) -> list[tuple[str, Any]]:
    return opener.conn.executed


# --- GET /push/key ----------------------------------------------------------------------------


def test_the_key_is_503_until_configured_then_the_derived_point(client: Any, monkeypatch, service) -> None:
    res = client.get("/push/key")
    assert res.status_code == 503 and res.json() == {"detail": "push is not configured"}
    raw = push.b64url(os.urandom(32))
    monkeypatch.setenv(push.VAPID_ENV, raw)
    res = client.get("/push/key")
    assert res.status_code == 200 and res.json() == {"public_key": push.public_key_b64url(raw)}
    point = push.unb64url(res.json()["public_key"])
    assert len(point) == 65 and point[0] == 4 and "=" not in res.json()["public_key"]
    assert service.opened == 0, "the key needs no connection"


def test_an_unreadable_key_is_503_and_the_log_never_prints_it(client: Any, monkeypatch, caplog) -> None:
    monkeypatch.setenv(push.VAPID_ENV, "-----BEGIN EC PRIVATE KEY----- s3cr3t")
    with caplog.at_level(logging.WARNING, logger="api.routes.push"):
        res = client.get("/push/key")
    assert res.status_code == 503
    assert "s3cr3t" not in caplog.text and "VAPID_PRIVATE_KEY is set but unreadable" in caplog.text


def test_the_key_needs_a_signed_in_caller(monkeypatch) -> None:
    monkeypatch.delenv("API_AUTH_OPTIONAL", raising=False)
    monkeypatch.setenv(push.VAPID_ENV, push.b64url(os.urandom(32)))
    assert TestClient(api_main.app).get("/push/key").status_code == 401


# --- PUT /push/subscription -------------------------------------------------------------------


@pytest.mark.parametrize(("change", "detail"), [
    ({"endpoint": "https://fcm.googleapis.com/fcm/send/" + "x" * 40}, "endpoint is not an Apple push endpoint"),
    ({"endpoint": ENDPOINT.replace("https", "http")}, "endpoint is not an Apple push endpoint"),
    ({"endpoint": ENDPOINT.replace("web.push", "u:p@web.push")}, "endpoint is not an Apple push endpoint"),
    ({"endpoint": ENDPOINT.replace(".com/", ".com:8443/")}, "endpoint is not an Apple push endpoint"),
    ({"endpoint": ENDPOINT + "x" * 1024}, "endpoint is not an Apple push endpoint"),
    ({"endpoint": ENDPOINT.replace("web", "WEB")}, "endpoint is not an Apple push endpoint"),
    ({"keys": {"p256dh": P256DH[:-2], "auth": AUTH}}, "keys.p256dh is not a P-256 public key"),
    ({"keys": {"p256dh": push.b64url(b"\x04" + b"\x01" * 64), "auth": AUTH}},
     "keys.p256dh is not a P-256 public key"),
    ({"keys": {"p256dh": push.b64url(b"\x05" + b"\x01" * 64), "auth": AUTH}},
     "keys.p256dh is not a P-256 public key"),
    ({"keys": {"p256dh": "!" * 87, "auth": AUTH}}, "keys.p256dh is not a P-256 public key"),
    ({"keys": {"p256dh": P256DH, "auth": push.b64url(os.urandom(15))}}, "keys.auth is not a 16-byte secret"),
])
def test_a_refused_registration_names_the_field_never_its_value(
        client: Any, service: _Opener, change: dict[str, Any], detail: str) -> None:
    body = _body(**change)
    res = client.put("/push/subscription", json=body)
    assert res.status_code == 422 and res.json() == {"detail": detail}
    assert body["endpoint"] not in res.text and "push.apple.com" not in res.text
    assert service.opened == 0, "validation runs before any SQL"


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://localhost:5173",
                                    "chrome-extension://abc"])
def test_the_origin_must_be_an_allowed_https_app_origin(client: Any, service: _Opener, origin: Any) -> None:
    headers = {"Origin": origin} if origin else {}
    if origin is None:
        client.headers.pop("Origin")
    res = client.put("/push/subscription", json=_body(), headers=headers)
    assert res.status_code == 422 and res.json() == {"detail": "Origin is not an allowed https origin"}
    assert service.opened == 0


def test_a_registration_ends_other_accounts_upserts_its_own_and_keeps_ten(client: Any, service: _Opener) -> None:
    padded = P256DH + "="
    res = client.put("/push/subscription", json=_body(keys={"p256dh": padded, "auth": AUTH},
                                                      user_agent="u" * 900))
    assert res.status_code == 204 and res.content == b""
    assert service.opened == 1
    sql = _sql(service)
    assert [q for q, _ in sql] == [" ".join(s.split()) for s in (
        push_devices._END_OTHER_ACCOUNTS_SQL, push_devices._UPSERT_SQL, push_devices._CAP_SQL)]
    params = sql[1][1]
    assert params["account_id"] == _ME and params["endpoint"] == ENDPOINT and params["origin"] == ORIGIN
    assert params["p256dh"] == P256DH, "stored as the browser's unpadded base64url"
    assert params["auth"] == AUTH and len(params["user_agent"]) == 512 and params["cap"] == 10
    assert service.conn.tx == ["commit"], "one transaction"


def test_a_registration_another_account_won_meanwhile_is_tried_once_more(client: Any, service: _Opener) -> None:
    service.conn.script = [(lambda q: q.startswith("INSERT INTO push_subscriptions"), [])]
    assert client.put("/push/subscription", json=_body()).status_code == 204
    kinds = [q.split()[0] for q, _ in _sql(service)]
    assert kinds == ["UPDATE", "INSERT", "UPDATE", "INSERT", "UPDATE"]


def test_a_check_the_validation_missed_is_a_bare_422(client: Any, service: _Opener) -> None:
    error = psycopg.errors.CheckViolation(f'new row violates check constraint. Failing row contains ({ENDPOINT})')
    service.conn.script = [(lambda q: q.startswith("INSERT INTO push_subscriptions"), error)]
    res = client.put("/push/subscription", json=_body())
    assert res.status_code == 422 and res.json() == {"detail": "subscription refused"}
    assert "push.apple.com" not in res.text and service.conn.tx == ["rollback"]


def test_a_push_route_never_acts_for_another_account(client: Any, service: _Opener) -> None:
    acting = {"X-Act-As": str(_OTHER)}
    for method, path, body in (("PUT", "/push/subscription", _body()),
                               ("DELETE", "/push/subscription", {"endpoint": ENDPOINT, "reason": "signed_out"}),
                               ("POST", "/push/opened", {"delivery": 5})):
        res = client.request(method, path, json=body, headers=acting)
        assert res.status_code == 403, path
        assert res.json() == {"detail": "this route does not act for another account"}
    assert service.opened == 0


# --- DELETE /push/subscription and POST /push/opened ------------------------------------------


@pytest.mark.parametrize("reason", ["signed_out", "switched_off"])
def test_an_end_is_the_callers_own_row_with_its_reason(client: Any, service: _Opener, reason: str) -> None:
    res = client.request("DELETE", "/push/subscription", json={"endpoint": ENDPOINT, "reason": reason})
    assert res.status_code == 204 and res.content == b""
    [(sql, params)] = _sql(service)
    assert sql == " ".join(push_devices._END_OWN_SQL.split())
    assert params == {"account_id": _ME, "endpoint": ENDPOINT, "reason": reason}


def test_an_end_of_anything_else_is_still_204_and_runs_nothing(client: Any, service: _Opener) -> None:
    res = client.request("DELETE", "/push/subscription",
                         json={"endpoint": "https://evil.example/x", "reason": "signed_out"})
    assert res.status_code == 204 and _sql(service) == []
    res = client.request("DELETE", "/push/subscription", json={"endpoint": ENDPOINT, "reason": "gone"})
    assert res.status_code == 422, "gone and replaced are the server's words, never the browser's"


def test_an_open_stamps_the_callers_own_delivery(client: Any, service: _Opener) -> None:
    assert client.post("/push/opened", json={"delivery": 42}).status_code == 204
    [(sql, params)] = _sql(service)
    assert sql == " ".join(push_devices._OPENED_SQL.split())
    assert params == {"account_id": _ME, "delivery": 42}
    assert "opened_at IS NULL" in sql and "d.account_id = %(account_id)s" in sql
    for bad in (0, -1, 2 ** 63, "x"):
        assert client.post("/push/opened", json={"delivery": bad}).status_code == 422


def test_the_push_routes_are_mounted_as_a_users_not_an_admins() -> None:
    from tests.api._routes import method_routes

    paths = {(m, p) for m, p, _r in method_routes()}
    assert {("GET", "/push/key"), ("PUT", "/push/subscription"), ("DELETE", "/push/subscription"),
            ("POST", "/push/opened")} <= paths
