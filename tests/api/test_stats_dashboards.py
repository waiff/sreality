"""Named Stats dashboards and their read-only shares (migration 647), hermetic.

The routes run unmodified over a scripted tenant connection (tests/api/_script_conn.py): the
saved-set SQL the dashboards instance renders, the projection that tells own from shared, the
write-is-the-gate 404s a recipient gets, and the share route's order — the tenant gate first, the
service role only after it. The account resolver returns a distinctive sentinel (tenancy.md's
#917 rule). What only a database can show (the policies, the trigger, the counterparty labels) is
tests/test_stats_dashboard_shares_live.py.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

import psycopg  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from api import dependencies as deps  # noqa: E402
from api import main as api_main  # noqa: E402
from api import saved_sets as ss  # noqa: E402
from api import shares  # noqa: E402
from api import tenant_pool  # noqa: E402
from api.notifications import ON_EVENT_CONFLICT  # noqa: E402
from api.routes.filter_presets import PRESETS  # noqa: E402
from api.routes.stats_dashboards import DASHBOARDS  # noqa: E402
from tests.api._script_conn import ScriptConn  # noqa: E402

_SUB = "11111111-1111-1111-1111-111111111111"
_ACCT = uuid.UUID("44444444-4444-4444-4444-444444444444")
_RECIPIENT = uuid.UUID("33333333-3333-3333-3333-333333333333")
_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
_AT = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)

_CHART = {"id": "c1", "view": "box", "rows": ["disposition"], "cols": [], "measure": "ppm2"}
_NORMAL = {"id": "c1", "view": "box", "stats": ["median"], "rows": ["disposition"], "cols": [],
           "measure": "ppm2"}
_SPEC = {"filters": {"categoryMain": "byt", "districts": [
    {"name": "Brno", "context": None, "level": "obec", "id": 582786}]}, "sort": "-price_czk"}

# (id, name, charts, filter_spec, created_at, updated_at, position, color, owner_email, shared_with)
_OWN = (_ID, "Praha byty", [_NORMAL], _SPEC, _AT, _AT, 0, "sage", None,
        [{"account_id": str(_RECIPIENT), "email": "b@x.cz"}])
_OWN_PAYLOAD = {
    "id": str(_ID), "name": "Praha byty", "charts": [_NORMAL], "filter_spec": _SPEC,
    "created_at": _AT.isoformat(), "updated_at": _AT.isoformat(), "position": 0, "color": "sage",
    "owner_email": None, "shared_with": [{"account_id": str(_RECIPIENT), "email": "b@x.cz"}],
}


def _starts(prefix: str) -> Callable[[str], bool]:
    return lambda q: q.startswith(prefix)


@pytest.fixture()
def conn() -> ScriptConn:
    return ScriptConn([])


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, conn: ScriptConn) -> Iterator[Any]:
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"sub": _SUB}
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda c, claims: _ACCT)
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


# --- the saved-set CRUD, dashboards instance ----------------------------------


def test_save_as_dashboard_inserts_one_named_row_for_the_callers_account(client, conn) -> None:
    conn.script = [(_starts("INSERT INTO stats_dashboards"), [_OWN])]
    res = client.post("/stats/dashboards", json={
        "name": "Praha byty", "charts": [_CHART], "filter_spec": _SPEC, "color": "sage"})
    assert res.status_code == 200, res.text
    assert res.json() == _OWN_PAYLOAD
    [(sql, params)] = conn.executed
    assert sql.startswith(
        "INSERT INTO stats_dashboards AS t (name, charts, filter_spec, color, position, account_id) "
        "VALUES (%s, %s::jsonb, %s::jsonb, %s, "
        "(SELECT COALESCE(MAX(t.position), -1) + 1 FROM stats_dashboards t "
        "WHERE t.account_id IN (SELECT current_account_ids()) AND name IS NOT NULL), %s) "
        "RETURNING t.id, t.name, t.charts, t.filter_spec,")
    name, charts, spec, color, owner = params
    # The charts are the registry's normalised board; the filter is the preset blob, verbatim.
    assert (name, json.loads(charts), json.loads(spec), color) == ("Praha byty", [_NORMAL], _SPEC, "sage")
    assert owner == _ACCT


def test_a_new_dashboard_goes_last_among_the_callers_own_never_after_a_shared_one() -> None:
    """RLS shows a recipient the dashboards shared with them; the new row's position counts only their own.
    A preset is never shared, so its insert keeps the plain RLS-scoped MAX."""
    dash, preset = ss._insert_sql(DASHBOARDS), ss._insert_sql(PRESETS)
    assert ("(SELECT COALESCE(MAX(t.position), -1) + 1 FROM stats_dashboards t "
            "WHERE t.account_id IN (SELECT current_account_ids()) AND name IS NOT NULL)") in dash
    assert "(SELECT COALESCE(MAX(t.position), -1) + 1 FROM filter_presets t), %s)" in preset
    assert "current_account_ids" not in preset


def test_a_dashboard_without_a_filter_stores_sql_null_not_json_null(client, conn) -> None:
    conn.script = [(_starts("INSERT INTO stats_dashboards"), [_OWN])]
    assert client.post("/stats/dashboards", json={"name": "n", "charts": []}).status_code == 200
    [(_sql, params)] = conn.executed
    assert params[2] is None and params[3] is None


@pytest.mark.parametrize(("body", "detail"), [
    ({"name": "n", "charts": [{**_CHART, "view": "pie"}]}, "chart 0: unknown view 'pie'"),
    ({"name": "n", "charts": [_CHART, _CHART]}, "chart 1: duplicate id 'c1'"),
])
def test_a_board_the_registry_refuses_is_a_422_naming_the_chart(client, conn, body, detail) -> None:
    res = client.post("/stats/dashboards", json=body)
    assert res.status_code == 422 and res.json()["detail"] == detail
    res = client.put(f"/stats/dashboards/{_ID}", json={"charts": body["charts"]})
    assert res.status_code == 422 and res.json()["detail"] == detail
    assert conn.executed == []


@pytest.mark.parametrize("body", [
    {"name": "", "charts": []},
    {"name": "x" * 201, "charts": []},
    {"charts": []},
    {"name": "n"},
    {"name": "n", "charts": [], "color": "fuchsia"},
    {"name": "n", "charts": [], "filter_spec": {"filters": {"districts": [{"name": "Brno"}]}}},
    {"name": "n", "charts": [], "filter_spec": {"filters": {"districts": [
        {"name": "Brno", "level": "obec", "id": "582786"}]}}},
], ids=["blank name", "long name", "no name", "no charts", "off-palette colour", "uncoded chip",
        "chip id as a string"])
def test_a_bad_body_is_a_422_and_writes_nothing(client, conn, body) -> None:
    assert client.post("/stats/dashboards", json=body).status_code == 422
    assert conn.executed == []


def test_the_list_is_named_dashboards_own_and_shared_in_the_saved_order(client, conn) -> None:
    shared = (uuid.uuid4(), "Jejich", [], None, _AT, _AT, 3, None, "owner@x.cz",
              [{"account_id": str(_ACCT), "email": None}])
    conn.script = [(_starts("SELECT t.id"), [_OWN, shared])]
    res = client.get("/stats/dashboards")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2 and body["data"][0] == _OWN_PAYLOAD
    assert body["data"][1]["owner_email"] == "owner@x.cz" and body["data"][1]["filter_spec"] is None
    [(sql, params)] = conn.executed
    # Own dashboards first, then the shared ones, each group in its saved order (the SPA splits the same way).
    assert sql.endswith("FROM stats_dashboards t WHERE name IS NOT NULL "
                        "ORDER BY (t.account_id IN (SELECT current_account_ids())) DESC, "
                        "t.position ASC, t.created_at DESC")
    assert params is None


def test_no_route_of_the_kind_reaches_the_working_board() -> None:
    """Every saved-set statement carries the kind's scope: the unnamed row is GET/PUT /stats/dashboard's alone."""
    for sql in ss.statements(DASHBOARDS):
        assert "name IS NOT NULL" in sql, sql


def test_the_projection_tells_own_from_shared_by_owner_email_alone() -> None:
    proj = ss._projection(DASHBOARDS)
    # Dashboards never act (pipeline-sharing §3.7): "mine" is current_account_ids(), not the acting identity.
    assert ("CASE WHEN t.account_id IN (SELECT current_account_ids()) THEN NULL ELSE "
            "(SELECT e.email FROM share_party_emails() e WHERE e.account_id = t.account_id) END AS owner_email"
            ) in proj
    assert "acting_account_ids" not in proj
    assert ("FROM stats_dashboard_shares s WHERE s.dashboard_id = t.id) AS shared_with") in proj
    assert "(SELECT e.email FROM share_party_emails() e WHERE e.account_id = s.recipient_account_id)" in proj
    # The owner's account never leaves the row; only its counterparty label does.
    assert "t.account_id," not in proj and not proj.endswith("t.account_id")


def test_an_update_changes_what_was_sent_and_an_explicit_null_clears_the_filter(client, conn) -> None:
    conn.script = [(_starts("UPDATE stats_dashboards t SET"), [_OWN])]
    assert client.put(f"/stats/dashboards/{_ID}", json={"filter_spec": None, "color": None}).status_code == 200
    assert client.put(f"/stats/dashboards/{_ID}", json={"name": "Nové", "charts": [_CHART]}).status_code == 200
    (clear_sql, clear), (edit_sql, edit) = conn.executed
    assert clear_sql.startswith(
        "UPDATE stats_dashboards t SET filter_spec = %s::jsonb, color = %s, updated_at = now() "
        "WHERE t.id = %s AND name IS NOT NULL RETURNING")
    assert clear == [None, None, str(_ID)]
    # An omitted filter is KEPT (it is not in the SET); the charts are the registry's.
    assert edit_sql.startswith("UPDATE stats_dashboards t SET name = %s, charts = %s::jsonb, updated_at = now()")
    assert edit[0] == "Nové" and json.loads(edit[1]) == [_NORMAL] and edit[2] == str(_ID)


def test_a_null_name_or_board_keeps_the_stored_one() -> None:
    body = DASHBOARDS.update_in.model_validate({"name": None, "charts": None, "filter_spec": _SPEC})
    assert ss.changes(DASHBOARDS, body) == {"filter_spec": _SPEC}


def test_a_recipients_update_or_delete_matches_nothing_and_is_a_404(client, conn) -> None:
    """The write is the gate (tenancy.md shape 1): a shared row is visible, not owned."""
    res = client.put(f"/stats/dashboards/{_ID}", json={"name": "taken"})
    assert res.status_code == 404 and res.json()["detail"] == "dashboard not found"
    res = client.delete(f"/stats/dashboards/{_ID}")
    assert res.status_code == 404 and res.json()["detail"] == "dashboard not found"
    assert client.get(f"/stats/dashboards/{_ID}").status_code == 404
    assert [q.split(" ", 1)[0] for q, _ in conn.executed] == ["UPDATE", "DELETE", "SELECT"]
    assert not any("account_id = %s" in q for q, _ in conn.executed)


@pytest.mark.parametrize("body", [{}, {"name": None, "charts": None}], ids=["empty", "only nulls that keep"])
def test_a_put_that_changes_nothing_still_answers_only_to_the_owner(client, conn, body) -> None:
    """No field to write would fall back to a plain read, which a recipient passes (the shared-read arm).
    The row is locked FOR UPDATE instead, which applies the UPDATE policy: a recipient's PUT is the same 404
    a real change gets, and nothing is bumped."""
    res = client.put(f"/stats/dashboards/{_ID}", json=body)
    assert res.status_code == 404 and res.json()["detail"] == "dashboard not found"
    [(sql, params)] = conn.executed
    assert sql.startswith("SELECT t.id") and sql.endswith("WHERE t.id = %s AND name IS NOT NULL FOR UPDATE OF t")
    assert params == (str(_ID),)
    conn.script = [(lambda q: q.endswith("FOR UPDATE OF t"), [_OWN])]
    assert client.put(f"/stats/dashboards/{_ID}", json=body).json() == _OWN_PAYLOAD
    assert not any(q.startswith("UPDATE") for q, _ in conn.executed)


def test_update_refuses_a_field_the_kind_does_not_have(conn) -> None:
    with pytest.raises(AssertionError):
        ss.update(conn, DASHBOARDS, str(_ID), {"account_id": str(_ACCT)})
    assert conn.executed == []


@pytest.mark.parametrize(("method", "path", "body"), [
    ("get", "/stats/dashboards/42", None),
    ("put", "/stats/dashboards/42", {"name": "x"}),
    ("delete", "/stats/dashboards/42", None),
    ("put", "/stats/dashboards/reorder", {"ids": ["42"]}),
    ("post", "/stats/dashboards/42/shares", {"email": "b@x.cz"}),
    ("delete", f"/stats/dashboards/42/shares/{_RECIPIENT}", None),
])
def test_a_malformed_id_from_a_hand_edited_link_is_a_422(client, conn, method, path, body) -> None:
    call = getattr(client, method)
    assert (call(path, json=body) if body is not None else call(path)).status_code == 422
    assert conn.executed == []


def test_delete_and_reorder(client, conn) -> None:
    conn.script = [(_starts("DELETE FROM stats_dashboards"), [(1,)]),
                   (_starts("SELECT t.id"), [_OWN])]
    assert client.delete(f"/stats/dashboards/{_ID}").json() == {"deleted": True}
    res = client.put("/stats/dashboards/reorder", json={"ids": [str(_ID)]})
    assert res.status_code == 200 and res.json() == {"data": [_OWN_PAYLOAD], "total": 1}
    reorder = conn.executed[1]
    assert reorder[0] == (
        "UPDATE stats_dashboards t SET position = v.pos FROM (SELECT id, (ord - 1) AS pos "
        "FROM unnest(%s::uuid[]) WITH ORDINALITY AS u(id, ord)) v WHERE t.id = v.id AND name IS NOT NULL")
    assert reorder[1] == ([str(_ID)],)
    assert client.put("/stats/dashboards/reorder", json={"ids": []}).status_code == 422


# --- sharing ------------------------------------------------------------------


class _Opener:
    """Stands in for deps.open_background_conn; records whether the route ever opened it."""

    def __init__(self, conn: ScriptConn) -> None:
        self.conn, self.opened = conn, 0

    def __call__(self) -> Any:
        self.opened += 1
        return contextlib.nullcontext(self.conn)


_GATE = ("UPDATE stats_dashboards SET updated_at = now() "
         "WHERE id = %s AND account_id IN (SELECT current_account_ids()) AND name IS NOT NULL RETURNING name")


def _identity(*script: Any) -> ScriptConn:
    return ScriptConn([(lambda q: "FROM auth.users" in q, [(_RECIPIENT,)]), *script])


def test_a_share_of_a_dashboard_the_caller_does_not_own_or_never_named_is_404_before_the_service_role(
    client, conn, monkeypatch,
) -> None:
    opener = _Opener(_identity())
    monkeypatch.setattr(deps, "open_background_conn", opener)
    res = client.post(f"/stats/dashboards/{_ID}/shares", json={"email": "b@x.cz"})
    assert res.status_code == 404 and res.json()["detail"] == "dashboard not found"
    # The gate names the caller's OWN accounts and a NAMED row: a recipient, a delegate and the working
    # board all match nothing here (647's trigger refuses the working board again on the service role).
    assert [q for q, _ in conn.executed] == [_GATE]
    assert conn.executed[0][1] == (_ID,)
    assert opener.opened == 0, "a refused caller must not cost a service-role connection"


def test_a_new_share_writes_one_recipient_stamped_notification_and_returns_the_dashboard(
    client, conn, monkeypatch,
) -> None:
    conn.script = [(lambda q: q == _GATE, [("Praha byty",)]), (_starts("SELECT t.id"), [_OWN])]
    identity = _identity((_starts("INSERT INTO stats_dashboard_shares"), [(_ACCT,)]))
    monkeypatch.setattr(deps, "open_background_conn", _Opener(identity))
    res = client.post(f"/stats/dashboards/{_ID}/shares", json={"email": " B@X.cz "})
    assert res.status_code == 200, res.text
    assert res.json() == _OWN_PAYLOAD
    assert identity.executed[0][1] == ("b@x.cz",)
    share, dispatch = identity.executed[1], identity.executed[2]
    assert share == ("INSERT INTO stats_dashboard_shares (dashboard_id, recipient_account_id) VALUES (%s, %s) "
                     "ON CONFLICT DO NOTHING RETURNING account_id", (_ID, _RECIPIENT))
    sql, params = dispatch
    assert "message" not in sql and "account_email" not in sql
    assert sql.endswith(ON_EVENT_CONFLICT)
    # No collection and no text (F17, R11): the owner the trigger stamped is the actor (migration 652),
    # and the bell names the dashboard by its live name.
    assert params == {
        "change_kind": "shared_dashboard", "collection_id": None, "actor": _ACCT,
        "recipient": _RECIPIENT, "event_ref": str(_ID),
    }
    assert identity.tx == ["commit"]
    # The gate ran first, and the answer is re-read on the tenant connection (with shared_with).
    assert conn.executed[0][0] == _GATE and conn.executed[-1][0].startswith("SELECT t.id")


def test_a_re_share_writes_no_second_notification(client, conn, monkeypatch) -> None:
    conn.script = [(lambda q: q == _GATE, [("Praha byty",)]), (_starts("SELECT t.id"), [_OWN])]
    identity = _identity()
    monkeypatch.setattr(deps, "open_background_conn", _Opener(identity))
    assert client.post(f"/stats/dashboards/{_ID}/shares", json={"email": "b@x.cz"}).status_code == 200
    assert sum(q.startswith("INSERT INTO stats_dashboard_shares") for q, _ in identity.executed) == 1
    assert not any("notification_dispatches" in q for q, _ in identity.executed)


def test_a_revoke_then_re_share_notifies_no_second_time(client, conn, monkeypatch) -> None:
    """The share row is new again after a revoke, so the grant reaches its dispatch again, with the SAME identity
    (no time in it) and ON CONFLICT on it: migration 653's identity index keeps the one notification, so a
    revoke + re-share loop cannot refill the recipient's bell with text the owner chose."""
    import re

    from api.notifications import EVENT_IDENTITY
    from tests.migration_defs import MIGRATIONS

    keys: list[Any] = []
    for _round in range(2):
        conn.script = [(lambda q: q == _GATE, [("Praha byty",)]), (_starts("SELECT t.id"), [_OWN])]
        identity = _identity((_starts("INSERT INTO stats_dashboard_shares"), [(_ACCT,)]))
        monkeypatch.setattr(deps, "open_background_conn", _Opener(identity))
        assert client.post(f"/stats/dashboards/{_ID}/shares", json={"email": "b@x.cz"}).status_code == 200
        [(sql, params)] = [(q, p) for q, p in identity.executed if "notification_dispatches" in q]
        assert sql.endswith(ON_EVENT_CONFLICT)
        keys.append((params["change_kind"], params["collection_id"], params["event_ref"], params["actor"],
                     params["recipient"]))
        revoke = ScriptConn([(_starts("DELETE FROM stats_dashboard_shares"), [(1,)])])
        assert shares.revoke_dashboard(revoke, str(_ID), str(_RECIPIENT)) == {"deleted": True}
    assert keys == [("shared_dashboard", None, str(_ID), _ACCT, _RECIPIENT)] * 2
    s653 = " ".join((MIGRATIONS / "653_notifications_w3_event_identity.sql").read_text().split())
    assert re.search(r"create unique index concurrently if not exists notification_dispatches_identity_uq "
                     rf"on public\.notification_dispatches \({', '.join(EVENT_IDENTITY)}\) nulls not distinct", s653)


def test_sharing_with_your_own_account_is_422_and_rolled_back(client, conn, monkeypatch) -> None:
    conn.script = [(lambda q: q == _GATE, [("Praha byty",)])]
    identity = _identity((_starts("INSERT INTO stats_dashboard_shares"),
                          psycopg.errors.CheckViolation("stats_dashboard_shares_not_self")))
    monkeypatch.setattr(deps, "open_background_conn", _Opener(identity))
    res = client.post(f"/stats/dashboards/{_ID}/shares", json={"email": "me@x.cz"})
    assert res.status_code == 422 and res.json()["detail"] == "That's your own account."
    assert identity.tx == ["rollback"]


def test_an_unknown_email_is_422(client, conn, monkeypatch) -> None:
    conn.script = [(lambda q: q == _GATE, [("Praha byty",)])]
    identity = ScriptConn([(lambda q: "FROM auth.users" in q, [])])
    monkeypatch.setattr(deps, "open_background_conn", _Opener(identity))
    res = client.post(f"/stats/dashboards/{_ID}/shares", json={"email": "nobody@x.cz"})
    assert res.status_code == 422 and res.json()["detail"] == "No account uses this email."
    assert not any(q.startswith("INSERT") for q, _ in identity.executed)


def test_sharing_is_never_delegated_and_needs_an_account(client, conn, monkeypatch) -> None:
    granted: list[Any] = []
    monkeypatch.setattr(shares, "grant", lambda opener, email, share: granted.append(share))
    for path, body in ((f"/stats/dashboards/{_ID}/shares", {"email": "b@x.cz"}),
                       ("/stats/dashboards", {"name": "n", "charts": []})):
        res = client.post(path, json=body, headers={"X-Act-As": str(uuid.uuid4())})
        assert res.status_code == 403
        assert res.json()["detail"] == "this route does not act for another account"
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda c, claims: None)
    for path, body in ((f"/stats/dashboards/{_ID}/shares", {"email": "b@x.cz"}),
                       ("/stats/dashboards", {"name": "n", "charts": []})):
        res = client.post(path, json=body)
        assert res.status_code == 400 and res.json()["detail"] == "no account for caller"
    assert granted == [] and conn.executed == []


def test_revoke_or_leave_is_one_delete_and_404_when_it_matches_nothing(client, conn) -> None:
    path = f"/stats/dashboards/{_ID}/shares/{_RECIPIENT}"
    assert client.delete(path).status_code == 404
    [(sql, params)] = conn.executed
    assert sql == "DELETE FROM stats_dashboard_shares WHERE dashboard_id = %s AND recipient_account_id = %s"
    assert params == (str(_ID), str(_RECIPIENT))
    conn.script = [(_starts("DELETE FROM stats_dashboard_shares"), [(1,)])]
    assert client.delete(path).json() == {"deleted": True}
    assert client.delete(f"/stats/dashboards/{_ID}/shares/not-a-uuid").status_code == 422


def test_the_share_body_is_the_one_email_rule() -> None:
    from api import schemas as s

    assert s.ShareDashboardIn is s.ShareIn
    assert s.ShareDashboardIn(email="  B@X.cz ").email == "b@x.cz"


# --- the routes' posture --------------------------------------------------------


def _dashboard_routes() -> dict[str, APIRoute]:
    out: dict[str, APIRoute] = {}

    def walk(routes: Any) -> None:
        for r in routes:
            if isinstance(r, APIRoute):
                if r.path.startswith("/stats/dashboard"):
                    out.update({f"{m} {r.path}": r for m in r.methods})
                continue
            nested = getattr(getattr(r, "original_router", None), "routes", None)
            walk(nested if nested is not None else getattr(r, "routes", None) or [])

    walk(api_main.app.routes)
    return out


def _calls(route: APIRoute) -> set:
    out, stack = set(), list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        if d.call is not None:
            out.add(d.call)
        stack.extend(d.dependencies)
    return out


def test_every_dashboard_route_rides_the_tenant_connection_and_the_writes_that_insert_name_an_account():
    routes = _dashboard_routes()
    assert set(routes) == {
        "GET /stats/dashboards", "POST /stats/dashboards", "PUT /stats/dashboards/reorder",
        "GET /stats/dashboards/{dashboard_id}", "PUT /stats/dashboards/{dashboard_id}",
        "DELETE /stats/dashboards/{dashboard_id}", "POST /stats/dashboards/{dashboard_id}/shares",
        "DELETE /stats/dashboards/{dashboard_id}/shares/{account_id}",
        "GET /stats/dashboard", "PUT /stats/dashboard",
    }
    # The working-board GET is a tenant read (shape 1): RLS alone, no account.
    own = {"POST /stats/dashboards", "POST /stats/dashboards/{dashboard_id}/shares", "PUT /stats/dashboard"}
    for key, route in routes.items():
        calls = _calls(route)
        assert tenant_pool.tenant_conn in calls, f"{key} left the tenant connection"
        assert deps.get_db_conn not in calls and deps.require_token not in calls, key
        assert (tenant_pool.require_account_id in calls) == (key in own), key
        assert tenant_pool.require_acting_account_id not in calls, f"{key}: a dashboard never acts"


def test_create_takes_its_account_as_a_required_keyword() -> None:
    import inspect

    param = inspect.signature(ss.create).parameters["account_id"]
    assert param.default is inspect.Parameter.empty and param.kind is inspect.Parameter.KEYWORD_ONLY
