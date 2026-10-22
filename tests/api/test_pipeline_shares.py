"""Pipeline sharing and acting as the owner (migration 641), hermetic.

The share module (api/shares.py) against a scripted connection; the two write-account
dependencies (api/tenant_pool.py) on their own and through the routes. The database half
(policies, the ledger, fail-closed) is tests/test_pipeline_shares_live.py.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

import psycopg  # noqa: E402

from api import dependencies as deps  # noqa: E402
from api import main as api_main  # noqa: E402
from api import pipeline as pipeline_module  # noqa: E402
from api import schemas as s  # noqa: E402
from api import shares  # noqa: E402
from api import tenant_pool  # noqa: E402
from api.notifications import ON_EVENT_CONFLICT  # noqa: E402
from tests.api._script_conn import ScriptConn  # noqa: E402

_SUB = "11111111-1111-1111-1111-111111111111"
_ME = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OWNER = uuid.UUID("44444444-4444-4444-4444-444444444444")
_RECIPIENT = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _Opener:
    def __init__(self, conn: ScriptConn) -> None:
        self.conn, self.opened = conn, 0

    def __call__(self) -> Any:
        self.opened += 1
        return contextlib.nullcontext(self.conn)


def _lookup(rows: list[tuple[Any, ...]]) -> tuple[Any, Any]:
    return (lambda q: "FROM auth.users" in q, rows)


def _upsert(rows: Any) -> tuple[Any, Any]:
    return (lambda q: q.startswith("INSERT INTO pipeline_shares"), rows)


def _dispatches(conn: ScriptConn) -> list[tuple[str, Any]]:
    return [(q, p) for q, p in conn.executed if "notification_dispatches" in q]


# --- api/shares.py ----------------------------------------------------------


def test_an_unknown_email_is_422_and_writes_nothing():
    identity = ScriptConn([_lookup([])])
    with pytest.raises(fastapi.HTTPException) as exc:
        shares.grant(_Opener(identity), "nobody@x.cz", shares.PipelineShare(_OWNER, "read"))
    assert exc.value.status_code == 422 and exc.value.detail == "No account uses this email."
    assert not any(q.startswith("INSERT") for q, _ in identity.executed)


def test_sharing_with_yourself_is_422_and_rolled_back():
    identity = ScriptConn([
        _lookup([(_OWNER,)]),
        _upsert(psycopg.errors.CheckViolation("pipeline_shares_not_self")),
    ])
    with pytest.raises(fastapi.HTTPException) as exc:
        shares.grant(_Opener(identity), "me@x.cz", shares.PipelineShare(_OWNER, "edit"))
    assert exc.value.status_code == 422 and exc.value.detail == "That's your own account."
    assert identity.tx == ["rollback"] and not _dispatches(identity)


def test_a_new_pipeline_share_upserts_the_mode_and_notifies_once_per_owner_recipient_mode():
    identity = ScriptConn([_lookup([(_RECIPIENT,)]), _upsert([(True,)])])
    shares.grant(_Opener(identity), "b@x.cz", shares.PipelineShare(_OWNER, "read"))
    upsert = next((q, p) for q, p in identity.executed if q.startswith("INSERT INTO pipeline_shares"))
    assert upsert[1] == (_OWNER, _RECIPIENT, "read")
    assert "ON CONFLICT (account_id, recipient_account_id) DO UPDATE SET mode = EXCLUDED.mode" in upsert[0]
    assert "WHERE s.mode <> EXCLUDED.mode" in upsert[0]
    [(sql, params)] = _dispatches(identity)
    assert "VALUES ('share', %(collection_id)s::bigint, %(change_kind)s::text," in sql
    assert sql.endswith(ON_EVENT_CONFLICT) and "dedupe_key" not in sql
    # The kind names what opens, the actor is the owner (migration 652), the mode tells two apart.
    assert "message" not in sql
    assert params == {
        "change_kind": "shared_pipeline", "collection_id": None, "actor": _OWNER,
        "recipient": _RECIPIENT, "event_ref": "read",
    }
    assert identity.tx == ["commit"]


def test_widening_to_edit_notifies_and_narrowing_or_repeating_does_not():
    widened = ScriptConn([_lookup([(_RECIPIENT,)]), _upsert([(False,)])])
    shares.grant(_Opener(widened), "b@x.cz", shares.PipelineShare(_OWNER, "edit"))
    [(_sql, params)] = _dispatches(widened)
    assert params["event_ref"] == "edit" and params["actor"] == _OWNER

    narrowed = ScriptConn([_lookup([(_RECIPIENT,)]), _upsert([(False,)])])
    shares.grant(_Opener(narrowed), "b@x.cz", shares.PipelineShare(_OWNER, "read"))
    assert not _dispatches(narrowed)

    same = ScriptConn([_lookup([(_RECIPIENT,)]), _upsert([])])
    shares.grant(_Opener(same), "b@x.cz", shares.PipelineShare(_OWNER, "edit"))
    assert not _dispatches(same)


def test_the_list_splits_my_recipients_from_the_boards_shared_with_me():
    conn = ScriptConn([(lambda q: "FROM pipeline_shares s" in q, [
        (True, _RECIPIENT, "edit", "b@x.cz"),
        (False, _OWNER, "read", "owner@x.cz"),
    ])])
    assert shares.list_pipeline_shares(conn) == {
        "shared_with": [{"account_id": str(_RECIPIENT), "email": "b@x.cz", "mode": "edit"}],
        "shared_with_me": [{"account_id": str(_OWNER), "email": "owner@x.cz", "mode": "read"}],
    }
    sql = conn.executed[0][0]
    assert "share_party_emails()" in sql and "%s" not in sql


@pytest.mark.parametrize(("side", "named"), [
    ("shared_with", "recipient_account_id = %s AND account_id IN (SELECT current_account_ids())"),
    ("shared_with_me", "account_id = %s AND recipient_account_id IN (SELECT current_account_ids())"),
])
def test_a_revoke_names_one_side_so_a_mutual_pair_loses_one_grant(side: str, named: str):
    conn = ScriptConn([(lambda q: q.startswith("DELETE FROM pipeline_shares"), [(1,)])])
    assert shares.revoke_pipeline(conn, str(_RECIPIENT), side) == {"deleted": True}  # type: ignore[arg-type]
    [(sql, params)] = conn.executed
    assert named in sql and params == (str(_RECIPIENT),)

    with pytest.raises(fastapi.HTTPException) as exc:
        shares.revoke_pipeline(ScriptConn([]), str(_RECIPIENT), side)  # type: ignore[arg-type]
    assert exc.value.status_code == 404


def test_the_pipeline_share_body_reuses_the_one_email_rule():
    body = s.SharePipelineIn(email="  B@X.cz ", mode="edit")
    assert body.email == "b@x.cz" and body.mode == "edit"
    with pytest.raises(ValueError):
        s.SharePipelineIn(email="nope", mode="read")
    with pytest.raises(ValueError):
        s.SharePipelineIn(email="b@x.cz", mode="admin")


# --- the two write-account dependencies -------------------------------------


def test_the_header_must_be_an_account_id():
    assert tenant_pool.act_as_header(None) is None
    assert tenant_pool.act_as_header("  ") is None
    assert tenant_pool.act_as_header(str(_OWNER)) == _OWNER
    with pytest.raises(fastapi.HTTPException) as exc:
        tenant_pool.act_as_header("not-a-uuid")
    assert exc.value.status_code == 400


def test_an_own_account_route_refuses_the_header_before_any_sql():
    conn = ScriptConn([])
    with pytest.raises(fastapi.HTTPException) as exc:
        tenant_pool.require_account_id(conn, {"sub": _SUB}, _OWNER)
    assert exc.value.status_code == 403
    assert exc.value.detail == "this route does not act for another account"
    assert conn.executed == []


def test_an_acting_route_without_the_header_is_the_callers_own_account(monkeypatch):
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda conn, claims: _ME)
    assert tenant_pool.require_acting_account_id(ScriptConn([]), {"sub": _SUB}, None) == _ME


def test_an_acting_route_under_the_header_is_the_owner_the_database_grants(monkeypatch):
    def _raise(conn: Any, claims: Any) -> Any:
        raise AssertionError("under the header the identity is the database's, never membership")

    monkeypatch.setattr(tenant_pool, "resolve_account_id", _raise)
    conn = ScriptConn([(lambda q: "FROM acting_account_ids()" in q, [(_OWNER,)])])
    assert tenant_pool.require_acting_account_id(conn, {"sub": _SUB}, _OWNER) == _OWNER

    for rows in ([], [(_OWNER,), (_ME,)]):
        conn = ScriptConn([(lambda q: "FROM acting_account_ids()" in q, rows)])
        with pytest.raises(fastapi.HTTPException) as exc:
            tenant_pool.require_acting_account_id(conn, {"sub": _SUB}, _OWNER)
        assert exc.value.status_code == 403
        assert exc.value.detail == "no edit share from that account"


# --- routes -------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    conn = ScriptConn([(lambda q: "FROM acting_account_ids()" in q, [(_OWNER,)])])
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"sub": _SUB}
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda c, claims: _ME)
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_a_card_write_acts_for_the_owner_under_the_header(client, monkeypatch):
    seen: list[Any] = []
    monkeypatch.setattr(
        pipeline_module, "add_card",
        lambda conn, body, *, account_id: seen.append(account_id) or {"added": True},
    )
    assert client.post("/pipeline/cards", json={"property_id": 1}).status_code == 200
    assert client.post("/pipeline/cards", json={"property_id": 1},
                       headers={"X-Act-As": str(_OWNER)}).status_code == 200
    assert seen == [_ME, _OWNER]
    res = client.post("/pipeline/cards", json={"property_id": 1}, headers={"X-Act-As": "x"})
    assert res.status_code == 400


def test_sharing_is_never_delegated(client, monkeypatch):
    granted: list[Any] = []
    monkeypatch.setattr(shares, "grant", lambda opener, email, share: granted.append(share))
    monkeypatch.setattr(
        shares, "list_pipeline_shares", lambda conn: {"shared_with": [], "shared_with_me": []},
    )
    res = client.post("/pipeline/shares", json={"email": "b@x.cz", "mode": "edit"},
                      headers={"X-Act-As": str(_OWNER)})
    assert res.status_code == 403 and granted == []

    res = client.post("/pipeline/shares", json={"email": " B@x.cz ", "mode": "edit"})
    assert res.status_code == 200
    assert res.json() == {"shared_with": [], "shared_with_me": []}
    assert granted == [shares.PipelineShare(_ME, "edit")]
    assert client.post("/pipeline/shares", json={"email": "b@x.cz", "mode": "x"}).status_code == 422



def test_a_delegate_cannot_share_the_owners_collection(client, monkeypatch):
    from api import curation

    created: list[Any] = []
    monkeypatch.setattr(curation, "create_share", lambda *a: created.append(a) or {"id": 1})
    res = client.post("/collections/1/shares", json={"email": "c@x.cz"},
                      headers={"X-Act-As": str(_OWNER)})
    assert res.status_code == 403 and created == []
    assert client.post("/collections/1/shares", json={"email": "c@x.cz"}).status_code == 200
    assert len(created) == 1


def test_a_delegate_cannot_switch_on_the_owners_collection_monitoring(client, monkeypatch):
    from api import curation

    calls: list[str] = []
    monkeypatch.setattr(curation, "create_collection",
                        lambda conn, body, account_id: calls.append("create") or {"id": 1})
    monkeypatch.setattr(curation, "update_collection",
                        lambda conn, cid, body: calls.append("update") or {"id": 1})
    acting = {"X-Act-As": str(_OWNER)}
    assert client.post("/collections", json={"name": "n", "monitoring_enabled": True},
                       headers=acting).status_code == 403
    assert client.patch("/collections/1", json={"monitoring_enabled": False},
                        headers=acting).status_code == 403
    assert calls == []
    assert client.post("/collections", json={"name": "n"}, headers=acting).status_code == 200
    assert client.patch("/collections/1", json={"name": "m"}, headers=acting).status_code == 200
    assert client.post("/collections", json={"name": "n", "monitoring_enabled": True}).status_code == 200
    assert calls == ["create", "update", "create"]

def test_the_share_routes_reach_the_module(client, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        shares, "list_pipeline_shares", lambda conn: {"shared_with": [], "shared_with_me": []},
    )

    def fake_revoke(conn: Any, party: str, side: str) -> dict[str, Any]:
        seen["revoke"] = (party, side)
        return {"deleted": True}

    monkeypatch.setattr(shares, "revoke_pipeline", fake_revoke)
    assert client.get("/pipeline/shares").json() == {"shared_with": [], "shared_with_me": []}
    path = f"/pipeline/shares/{_RECIPIENT}"
    assert client.delete(f"{path}?side=shared_with").json() == {"deleted": True}
    assert seen["revoke"] == (str(_RECIPIENT), "shared_with")
    assert client.delete(path).status_code == 422, "the side is required: a mutual pair is two grants"
    assert client.delete(f"{path}?side=both").status_code == 422
    assert client.delete("/pipeline/shares/not-a-uuid?side=shared_with").status_code == 422
