"""Tests for the /filter-presets endpoints (Browse saved filter presets).

Hermetic — overrides the tenant connection + JWT (the routes are per account
since ticket #1761) so no real DB is hit, and patches the saved-set persistence
functions (api/saved_sets.py, the presets instance) with in-memory fakes. Pydantic
validation (blank name, missing filter_spec) and the PUT's field rule
(`saved_sets.changes`) run unmodified through the route layer. The account
resolver is stubbed to a distinctive sentinel, never to None (tenancy.md's
#917 rule), and the tests assert the sentinel ARRIVES at the insert.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import saved_sets as ss
from api import tenant_pool
from api.routes.filter_presets import GROUPS, PRESETS
from tests.api._routes import flat_routes, reachable_calls

_SUB = "11111111-1111-1111-1111-111111111111"
_ACCT = "33333333-3333-3333-3333-333333333333"


def _id(n: int) -> str:
    """The n-th preset the fake store mints: ids are uuids at the route edge (a malformed one is a 422)."""
    return f"00000000-0000-0000-0000-{n:012d}"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"sub": _SUB}
    monkeypatch.setattr(
        tenant_pool, "resolve_account_id", lambda conn, claims: _ACCT,
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory replacement for the saved-set persistence functions: presets and their groups."""
    state: dict[str, Any] = {"presets": {}, "groups": {}, "owners": {}, "next_id": 1}

    def _rows(kind: ss.SavedSetKind) -> dict[str, dict[str, Any]]:
        assert kind in (PRESETS, GROUPS)
        return state["presets"] if kind is PRESETS else state["groups"]

    def fake_create_preset(
        conn: Any, kind: ss.SavedSetKind, values: dict[str, Any], *, account_id: str,
    ) -> dict[str, Any]:
        rows = _rows(kind)
        n = state["next_id"]
        pid = _id(n)
        state["next_id"] += 1
        state["owners"][pid] = account_id
        rows[pid] = {
            "id": pid,
            "name": values["name"],
            **{c: values[c] for c in kind.blob_columns},
            "created_at": f"2026-06-03T00:00:0{n}+00:00",
            "updated_at": f"2026-06-03T00:00:0{n}+00:00",
            # Append at end: one past the current max position.
            "position": max((p["position"] for p in rows.values()), default=-1) + 1,
            "color": values["color"],
            **({"group_id": None} if kind.groups else {}),
        }
        return dict(rows[pid])

    def fake_list_presets(conn: Any, kind: ss.SavedSetKind) -> list[dict[str, Any]]:
        rows = [dict(r) for r in _rows(kind).values()]
        rows.sort(key=lambda r: (r["position"], r["created_at"]))
        return rows

    def fake_get_preset(conn: Any, kind: ss.SavedSetKind, pid: str) -> dict[str, Any] | None:
        rows = _rows(kind)
        return dict(rows[pid]) if pid in rows else None

    def fake_update_preset(
        conn: Any, kind: ss.SavedSetKind, pid: str, fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        # The route hands over only what changes (saved_sets.changes), so the fake applies it whole.
        rows = _rows(kind)
        if pid not in rows:
            return None
        rows[pid].update(fields)
        return dict(rows[pid])

    def fake_delete_preset(conn: Any, kind: ss.SavedSetKind, pid: str) -> bool:
        if _rows(kind).pop(pid, None) is None:
            return False
        # ON DELETE SET NULL (group_id): the group's presets fall loose.
        for p in state["presets"].values():
            if p["group_id"] == pid:
                p["group_id"] = None
        return True

    def fake_reorder_presets(conn: Any, kind: ss.SavedSetKind, ids: list[str]) -> None:
        rows = _rows(kind)
        for pos, pid in enumerate(ids):
            if pid in rows:
                rows[pid]["position"] = pos

    def fake_layout(
        conn: Any, kind: ss.SavedSetKind, groups: list[str], items: list[tuple[str, str | None]],
    ) -> dict[str, Any]:
        assert kind is PRESETS
        # The (group_id, account_id) key: a group this account does not hold refuses the whole write.
        if any(g is not None and g not in state["groups"] for _, g in items):
            raise ss.HTTPException(status_code=422, detail="group not found")
        fake_reorder_presets(conn, GROUPS, groups)
        for pos, (pid, gid) in enumerate(items):
            if pid in state["presets"]:
                state["presets"][pid].update(position=pos, group_id=gid)
        return ss.listing(conn, kind)

    monkeypatch.setattr(ss, "create", fake_create_preset)
    monkeypatch.setattr(ss, "list_rows", fake_list_presets)
    monkeypatch.setattr(ss, "get", fake_get_preset)
    monkeypatch.setattr(ss, "update", fake_update_preset)
    monkeypatch.setattr(ss, "delete", fake_delete_preset)
    monkeypatch.setattr(ss, "reorder", fake_reorder_presets)
    monkeypatch.setattr(ss, "layout", fake_layout)

    return state


SPEC = {"categoryMain": "byt", "categoryType": "prodej", "priceMax": 6_000_000}


def test_create_preset_returns_row(client, store):
    res = client.post("/filter-presets", json={"name": "Praha pod 6M", "filter_spec": SPEC})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == _id(1)
    assert body["name"] == "Praha pod 6M"
    assert body["filter_spec"] == SPEC


def test_create_preset_defaults_color_null(client, store):
    body = client.post(
        "/filter-presets", json={"name": "p", "filter_spec": SPEC}
    ).json()
    assert body["color"] is None


def test_create_preset_with_color(client, store):
    body = client.post(
        "/filter-presets", json={"name": "p", "filter_spec": SPEC, "color": "plum"}
    ).json()
    assert body["color"] == "plum"


def test_create_preset_invalid_color_422(client, store):
    res = client.post(
        "/filter-presets",
        json={"name": "p", "filter_spec": SPEC, "color": "fuchsia"},
    )
    assert res.status_code == 422


def test_update_preset_sets_and_clears_color(client, store):
    client.post(
        "/filter-presets", json={"name": "p", "filter_spec": SPEC, "color": "sage"}
    )
    # Recolour.
    assert client.put(f"/filter-presets/{_id(1)}", json={"color": "teal"}).json()["color"] == "teal"
    # Explicit null clears it.
    assert client.put(f"/filter-presets/{_id(1)}", json={"color": None}).json()["color"] is None


def test_update_preset_omitting_color_leaves_it(client, store):
    client.post(
        "/filter-presets", json={"name": "p", "filter_spec": SPEC, "color": "brick"}
    )
    # A name-only update must not wipe the colour (omitted != cleared).
    body = client.put(f"/filter-presets/{_id(1)}", json={"name": "renamed"}).json()
    assert body["name"] == "renamed"
    assert body["color"] == "brick"


def test_create_preset_blank_name_422(client, store):
    res = client.post("/filter-presets", json={"name": "", "filter_spec": SPEC})
    assert res.status_code == 422


def test_create_preset_missing_spec_422(client, store):
    res = client.post("/filter-presets", json={"name": "x"})
    assert res.status_code == 422


def test_create_preset_appends_at_end(client, store):
    """New presets append at MAX(position)+1, so list order is save order."""
    for name in ("a", "b", "c"):
        client.post("/filter-presets", json={"name": name, "filter_spec": SPEC})
    body = client.get("/filter-presets").json()
    assert body["total"] == 3
    assert [r["name"] for r in body["data"]] == ["a", "b", "c"]
    assert [r["position"] for r in body["data"]] == [0, 1, 2]


def test_reorder_presets_rewrites_order(client, store):
    for name in ("a", "b", "c"):
        client.post("/filter-presets", json={"name": name, "filter_spec": SPEC})
    # Move "c" (id 3) to the front, then "a" (id 1), then "b" (id 2).
    res = client.put("/filter-presets/reorder", json={"ids": [_id(3), _id(1), _id(2)]})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert [r["name"] for r in body["data"]] == ["c", "a", "b"]
    assert [r["position"] for r in body["data"]] == [0, 1, 2]
    # Order persists on a fresh list.
    assert [r["name"] for r in client.get("/filter-presets").json()["data"]] == [
        "c",
        "a",
        "b",
    ]


def test_reorder_unknown_id_is_noop_for_missing(client, store):
    client.post("/filter-presets", json={"name": "a", "filter_spec": SPEC})
    res = client.put("/filter-presets/reorder", json={"ids": [_id(999), _id(1)]})
    assert res.status_code == 200
    # The one real preset still resolves; the unknown id is ignored.
    assert [r["name"] for r in res.json()["data"]] == ["a"]


def test_reorder_presets_empty_422(client, store):
    assert client.put("/filter-presets/reorder", json={"ids": []}).status_code == 422


def test_get_preset_404_when_missing(client, store):
    assert client.get(f"/filter-presets/{_id(999)}").status_code == 404


def test_update_preset_renames_without_touching_spec(client, store):
    client.post("/filter-presets", json={"name": "old", "filter_spec": SPEC})
    res = client.put(f"/filter-presets/{_id(1)}", json={"name": "new"})
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "new"
    assert body["filter_spec"] == SPEC


def test_update_preset_replaces_spec(client, store):
    client.post("/filter-presets", json={"name": "p", "filter_spec": SPEC})
    new_spec = {"categoryMain": "dum", "categoryType": "prodej"}
    res = client.put(f"/filter-presets/{_id(1)}", json={"filter_spec": new_spec})
    assert res.status_code == 200
    assert res.json()["filter_spec"] == new_spec


_STREET = {"name": "Na Rybníčkách", "context": "Pardubice", "level": "ulice", "id": 666742}


@pytest.mark.parametrize("spec", [
    {"filters": {**SPEC, "districts": [{"name": "Brno", "context": None}]}, "sort": None},
    {"filters": {**SPEC, "districts": [_STREET, {"name": "Lipová", "level": "locality",
                                                  "id": 554782}]}},
    {"filters": {"districts": [{"name": "Praha", "level": "obec", "id": -1}]}},
    {**SPEC, "districts": [{"name": "Brno"}]},
    {"filters": {"districts": [{**_STREET, "id": "666742"}]}},
    {"filters": {"districts": [{**_STREET, "id": True}]}},
    {"filters": {"districts": [{**_STREET, "excluded": "true"}]}},
], ids=["name only", "stale locality beside a coded chip", "the old sentinel", "legacy bare blob",
        "id as a string", "id as a bool", "excluded as a string"])
def test_a_preset_holding_a_chip_without_a_code_is_a_422_and_writes_nothing(client, store, spec):
    """A chip is a level plus a RÚIAN code or it cannot exist — on a preset too,
    whose blob is stored verbatim, so strictly: a coercible "666742" or `true`
    would be stored as written. The legacy bare shape cannot bypass it."""
    assert client.post("/filter-presets", json={"name": "p", "filter_spec": spec}).status_code == 422
    assert store["presets"] == {}
    client.post("/filter-presets", json={"name": "p", "filter_spec": SPEC})
    assert client.put(f"/filter-presets/{_id(1)}", json={"filter_spec": spec}).status_code == 422
    assert store["presets"][_id(1)]["filter_spec"] == SPEC


def test_a_preset_with_coded_chips_is_stored_unchanged(client, store):
    spec = {"filters": {**SPEC, "districts": [_STREET, {**_STREET, "id": 447358,
                                                        "excluded": True}]}, "sort": "-price_czk"}
    res = client.post("/filter-presets", json={"name": "p", "filter_spec": spec})
    assert res.status_code == 200
    assert res.json()["filter_spec"] == spec
    assert client.put(f"/filter-presets/{_id(1)}", json={"filter_spec": spec}).json()["filter_spec"] == spec


@pytest.mark.parametrize("spec", [SPEC, {"filters": SPEC, "sort": None},
                                  {"filters": {**SPEC, "districts": None}}])
def test_a_preset_without_chips_is_untouched(client, store, spec):
    res = client.post("/filter-presets", json={"name": "p", "filter_spec": spec})
    assert res.status_code == 200
    assert res.json()["filter_spec"] == spec


def test_update_preset_404_when_missing(client, store):
    assert client.put(f"/filter-presets/{_id(999)}", json={"name": "x"}).status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), [
    ("get", "/filter-presets/1", None),
    ("put", "/filter-presets/not-a-uuid", {"name": "x"}),
    ("delete", "/filter-presets/1", None),
    ("put", "/filter-presets/reorder", {"ids": ["1"]}),
])
def test_a_malformed_id_is_a_422_not_a_cast_error(
    client: Any, store: dict[str, Any], method: str, path: str, body: dict[str, Any] | None,
) -> None:
    """A hand-edited link must not reach the database's uuid cast (a 500)."""
    call = getattr(client, method)
    assert (call(path, json=body) if body is not None else call(path)).status_code == 422


def test_a_stored_blob_that_is_not_an_object_reads_as_empty():
    """PostgREST can write a JSON null into the NOT NULL column; the SPA reads `spec.filters`."""
    row = (_id(1), "p", None, None, None, 0, None, None)
    assert ss._row(PRESETS, row)["filter_spec"] == {}


def test_delete_preset(client, store):
    client.post("/filter-presets", json={"name": "p", "filter_spec": SPEC})
    assert client.delete(f"/filter-presets/{_id(1)}").json() == {"deleted": True}
    assert client.delete(f"/filter-presets/{_id(1)}").status_code == 404


# --- groups (migration 672) --------------------------------------------------


def test_the_list_carries_the_groups_and_each_preset_its_group(client, store):
    client.post("/filter-presets", json={"name": "a", "filter_spec": SPEC})
    group = client.post("/filter-preset-groups", json={"name": "Praha", "color": "sage"}).json()
    assert {k: group[k] for k in ("name", "color", "position")} == {"name": "Praha", "color": "sage", "position": 0}
    assert "filter_spec" not in group
    body = client.get("/filter-presets").json()
    assert [g["name"] for g in body["groups"]] == ["Praha"]
    assert body["data"][0]["group_id"] is None


def test_a_layout_moves_presets_into_and_out_of_groups_in_one_write(client, store):
    for name in ("a", "b", "c"):
        client.post("/filter-presets", json={"name": name, "filter_spec": SPEC})
    g1 = client.post("/filter-preset-groups", json={"name": "Praha"}).json()["id"]
    g2 = client.post("/filter-preset-groups", json={"name": "Brno"}).json()["id"]
    a, b, c = _id(1), _id(2), _id(3)
    res = client.put("/filter-presets/layout", json={
        "groups": [g2, g1],
        "items": [{"id": c, "group_id": None}, {"id": a, "group_id": g1}, {"id": b, "group_id": g1}],
    })
    assert res.status_code == 200
    body = res.json()
    assert [g["name"] for g in body["groups"]] == ["Brno", "Praha"]
    assert [(r["name"], r["group_id"]) for r in body["data"]] == [("c", None), ("a", g1), ("b", g1)]
    # Out again: a back to loose, after c.
    body = client.put("/filter-presets/layout", json={
        "groups": [g2, g1],
        "items": [{"id": c}, {"id": a}, {"id": b, "group_id": g1}],
    }).json()
    assert [(r["name"], r["group_id"]) for r in body["data"]] == [("c", None), ("a", None), ("b", g1)]


def test_a_layout_naming_a_group_the_account_does_not_hold_is_a_422_and_moves_nothing(client, store):
    client.post("/filter-presets", json={"name": "a", "filter_spec": SPEC})
    res = client.put("/filter-presets/layout", json={"items": [{"id": _id(1), "group_id": _id(999)}]})
    assert res.status_code == 422
    assert store["presets"][_id(1)]["group_id"] is None


def test_deleting_a_group_leaves_its_presets_loose(client, store):
    client.post("/filter-presets", json={"name": "a", "filter_spec": SPEC})
    gid = client.post("/filter-preset-groups", json={"name": "Praha"}).json()["id"]
    client.put("/filter-presets/layout", json={"items": [{"id": _id(1), "group_id": gid}]})
    assert client.delete(f"/filter-preset-groups/{gid}").json() == {"deleted": True}
    body = client.get("/filter-presets").json()
    assert body["groups"] == []
    assert [(r["name"], r["group_id"]) for r in body["data"]] == [("a", None)]


def test_a_group_renames_and_recolours_and_stamps_the_account(client, store):
    gid = client.post("/filter-preset-groups", json={"name": "Praha", "color": "plum"}).json()["id"]
    assert store["owners"][gid] == _ACCT
    body = client.put(f"/filter-preset-groups/{gid}", json={"name": "Praha 2"}).json()
    assert (body["name"], body["color"]) == ("Praha 2", "plum")
    assert client.put(f"/filter-preset-groups/{gid}", json={"color": None}).json()["color"] is None
    assert client.post("/filter-preset-groups", json={"name": ""}).status_code == 422
    assert client.post("/filter-preset-groups", json={"name": "x", "color": "fuchsia"}).status_code == 422


def test_a_layout_sql_writes_order_and_group_together():
    sql = ss._layout_sql(PRESETS)
    assert "SET position = v.pos, group_id = v.group_id" in sql
    assert "unnest(%s::uuid[], %s::uuid[]) WITH ORDINALITY" in sql
    # A kind without groups has no group column and no layout.
    from api.routes.stats_dashboards import DASHBOARDS
    assert "group_id" not in ss._list_sql(DASHBOARDS)
    assert len(ss.statements(DASHBOARDS)) == 7


# --- per account (ticket #1761) ---------------------------------------------


def test_create_preset_stamps_the_resolved_account(client, store):
    """The one insert names its owner: the account require_account_id resolved."""
    body = client.post("/filter-presets", json={"name": "p", "filter_spec": SPEC}).json()
    assert store["owners"][body["id"]] == _ACCT
    # The response shape is unchanged: the owner is not echoed back.
    assert "account_id" not in body


def test_create_preset_without_an_account_is_400(client, store, monkeypatch):
    monkeypatch.setattr(tenant_pool, "resolve_account_id", lambda conn, claims: None)
    res = client.post("/filter-presets", json={"name": "p", "filter_spec": SPEC})
    assert res.status_code == 400
    assert res.json()["detail"] == "no account for caller"
    assert store["presets"] == {}


def test_every_preset_route_rides_the_tenant_connection():
    """No preset or preset-group route accepts the static bundle token or the
    service-role connection: it proves only 'loaded the SPA', and RLS is the boundary."""
    routes = [r for r in flat_routes() if r.path.startswith("/filter-preset")]
    # Six saved-set routes per kind, plus the presets' layout.
    assert len(routes) == 13, [f"{sorted(r.methods)} {r.path}" for r in routes]
    for r in routes:
        calls = reachable_calls(r)
        label = f"{sorted(r.methods)} {r.path}"
        assert tenant_pool.tenant_conn in calls, f"{label} left the tenant connection"
        assert deps.require_token not in calls, f"{label} accepts the bundle token again"
        assert deps.get_db_conn not in calls, f"{label} reads on the service role again"


class _Cursor:
    def __init__(self, log: list) -> None:
        self.log = log
        self.rowcount = 1

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((sql, params))

    def fetchone(self) -> tuple:
        return ("p-1", "p", SPEC, None, None, 0, None, None)


class _Conn:
    def __init__(self) -> None:
        self.log: list = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)


def test_create_preset_sql_binds_the_account():
    """The persistence layer writes the owner into the row, and takes it as a
    REQUIRED keyword (no default: a dropped argument is a TypeError, not a NULL)."""
    import inspect

    param = inspect.signature(ss.create).parameters["account_id"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY

    conn = _Conn()
    ss.create(conn, PRESETS, {"name": "p", "filter_spec": SPEC, "color": None}, account_id=_ACCT)
    sql, params = conn.log[0]
    assert "account_id" in sql.split("VALUES")[0]
    assert params[-1] == _ACCT


def test_reads_and_writes_by_id_carry_no_account_predicate():
    """Doctrine shape 1: on the tenant connection RLS alone scopes a read and an
    UPDATE/DELETE by id; an explicit account predicate would be a second
    definition of the caller (the #917 bug), not a gate."""
    import inspect

    for fn in (ss.list_rows, ss.get, ss.update, ss.delete, ss.reorder, ss.changes, ss.listing, ss.layout):
        assert "account_id" not in inspect.getsource(fn), fn.__name__
    for sql in (ss._list_sql(PRESETS), ss._get_sql(PRESETS), ss._get_for_update_sql(PRESETS), ss._delete_sql(PRESETS),
                ss._update_sql(PRESETS, ("name", "filter_spec", "color")), ss._reorder_sql(PRESETS),
                ss._layout_sql(PRESETS), *ss.statements(GROUPS)[:3], *ss.statements(GROUPS)[4:]):
        assert "account_id" not in sql, sql
