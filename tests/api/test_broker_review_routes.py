"""Broker merge-review route tests — hermetic (no DB)."""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import broker_review as routes


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_admin] = (
        lambda: {"is_admin": True, "legacy": True}
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_candidates_list(client, monkeypatch):
    monkeypatch.setattr(routes.review, "list_candidates",
                        lambda conn, **kw: {"candidates": [{"id": 1}], "count": 1})
    res = client.get("/broker-review/candidates")
    assert res.status_code == 200
    assert res.json()["count"] == 1


def test_merge_candidate_passes_subset(client, monkeypatch):
    captured = {}
    def fake(conn, cid, *, broker_ids=None, created_by=None):
        captured["cid"], captured["ids"] = cid, broker_ids
        return {"merge_group_id": "g", "survivor_broker_id": 1, "retired_broker_ids": [2]}
    monkeypatch.setattr(routes.review, "merge_candidate", fake)
    res = client.post("/broker-review/candidates/5/merge", json={"broker_ids": [1, 2]})
    assert res.status_code == 200
    assert captured == {"cid": 5, "ids": [1, 2]}


def test_merge_candidate_404(client, monkeypatch):
    monkeypatch.setattr(routes.review, "merge_candidate", lambda conn, cid, **kw: None)
    assert client.post("/broker-review/candidates/9/merge", json={}).status_code == 404


def test_merge_candidate_conflict(client, monkeypatch):
    def boom(conn, cid, **kw):
        raise routes.review.MergeError("fewer than two active")
    monkeypatch.setattr(routes.review, "merge_candidate", boom)
    assert client.post("/broker-review/candidates/9/merge", json={}).status_code == 409


def test_dismiss_candidate(client, monkeypatch):
    monkeypatch.setattr(routes.review, "dismiss_candidate",
                        lambda conn, cid, **kw: {"id": cid, "status": "dismissed"})
    res = client.post("/broker-review/candidates/3/dismiss")
    assert res.status_code == 200 and res.json()["status"] == "dismissed"


def test_unmerge_404(client, monkeypatch):
    monkeypatch.setattr(routes.review, "unmerge_group", lambda conn, g, **kw: None)
    assert client.post("/broker-review/merges/abc/unmerge").status_code == 404


def test_unmerge_ok(client, monkeypatch):
    monkeypatch.setattr(routes.review, "unmerge_group",
                        lambda conn, g, **kw: {"merge_group_id": g, "survivor_broker_id": 1,
                                               "restored_broker_ids": [2]})
    res = client.post("/broker-review/merges/abc/unmerge")
    assert res.status_code == 200 and res.json()["restored_broker_ids"] == [2]


def test_candidates_reason_is_forwarded(client, monkeypatch):
    """The queue holds two generators at very different volumes (thousands of
    contact-bridge pairs per sweep against a few thousand name_firm groups) and the
    page is ordered by group size then recency, so unless the filter reaches the
    query one generator's regeneration buries the other's whole backlog below the
    single page the SPA fetches."""
    captured = {}
    monkeypatch.setattr(routes.review, "list_candidates",
                        lambda conn, **kw: captured.update(kw)
                        or {"candidates": [], "count": 0, "reason_counts": {}})
    assert client.get("/broker-review/candidates?reason=name_firm").status_code == 200
    assert captured["reason"] == "name_firm"
    client.get("/broker-review/candidates")
    assert captured["reason"] is None


def test_candidate_list_filters_by_reason_and_counts_the_whole_queue():
    """Through a fake cursor, because the shape is what regresses: a `reason` that
    never reaches the WHERE clause looks identical from a mocked route test."""
    from api import broker_review as review

    seen: list[tuple[str, object]] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): seen.append((sql, params))
        def fetchall(self):
            if "GROUP BY reason" in seen[-1][0]:
                return [{"reason": "name_firm", "n": 3187},
                        {"reason": "contact_bridge_review", "n": 9377}]
            return []

    class _Conn:
        def cursor(self, **kw): return _Cur()

    out = review.list_candidates(_Conn(), reason="name_firm", limit=100, offset=200)
    page_sql, page_params = seen[0]
    assert "reason = %(reason)s" in page_sql
    assert page_params["reason"] == "name_firm"
    assert (page_params["limit"], page_params["offset"]) == (100, 200)
    # counts span the whole status, not the page — that is what sizes the tab the
    # operator is NOT currently looking at
    assert out["reason_counts"] == {"name_firm": 3187, "contact_bridge_review": 9377}


def test_every_mutating_route_threads_the_admin_identity(client, monkeypatch):
    """require_admin returns the verified JWT claims and the routes bound them to
    `_`, so undone_by / resolved_by / created_by are NULL on every row written to
    date — and the suppression rows the rail now writes would have been anonymous
    too. Email is the readable handle; `sub` is the fallback."""
    captured: dict[str, Any] = {}
    api_main.app.dependency_overrides[deps.require_admin] = (
        lambda: {"is_admin": True, "email": "op@example.com", "sub": "uuid-1"}
    )
    monkeypatch.setattr(routes.review, "unmerge_group",
                        lambda conn, g, **kw: captured.update(unmerge=kw)
                        or {"merge_group_id": g, "survivor_broker_id": 1,
                            "restored_broker_ids": [2]})
    monkeypatch.setattr(routes.review, "dismiss_candidate",
                        lambda conn, cid, **kw: captured.update(dismiss=kw)
                        or {"id": cid, "status": "dismissed"})
    monkeypatch.setattr(routes.review, "merge_brokers",
                        lambda conn, ids, **kw: captured.update(merge=kw) or {"ok": 1})
    monkeypatch.setattr(routes.review, "merge_candidate",
                        lambda conn, cid, **kw: captured.update(candidate=kw) or {"ok": 1})

    posted = [
        client.post("/broker-review/merges/abc/unmerge"),
        client.post("/broker-review/candidates/3/dismiss"),
        client.post("/broker-review/merge", json={"broker_ids": [1, 2]}),
        client.post("/broker-review/candidates/3/merge", json={}),
    ]

    # a route that 4xx'd would "thread" nothing and the identity asserts below would
    # read a stale `captured` entry from an earlier post
    assert [r.status_code for r in posted] == [200, 200, 200, 200]
    assert captured["unmerge"]["undone_by"] == "op@example.com"
    assert captured["dismiss"]["resolved_by"] == "op@example.com"
    assert captured["merge"]["created_by"] == "op@example.com"
    assert captured["candidate"]["created_by"] == "op@example.com"


def test_the_actor_falls_back_to_the_subject_when_there_is_no_email():
    assert routes._actor({"sub": "uuid-1"}) == "uuid-1"
    assert routes._actor({"email": "op@example.com", "sub": "uuid-1"}) == "op@example.com"
    assert routes._actor({}) is None


def test_suppression_ledger_is_listable_with_paging_and_the_lifted_flag(client, monkeypatch):
    """Without a read surface the rail is a table only the nightly sweep can see."""
    captured = {}
    monkeypatch.setattr(routes.review, "list_suppressions",
                        lambda conn, **kw: captured.update(kw)
                        or {"suppressions": [{"id": 1}], "count": 1})
    res = client.get("/broker-review/suppressions")
    assert res.status_code == 200 and res.json()["count"] == 1
    assert captured == {"limit": 50, "offset": 0, "include_lifted": False}

    assert client.get(
        "/broker-review/suppressions?limit=10&offset=20&include_lifted=true"
    ).status_code == 200
    assert captured == {"limit": 10, "offset": 20, "include_lifted": True}


def test_lifting_a_suppression_threads_the_operator_and_the_reason(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(routes.review, "lift_suppression",
                        lambda conn, sid, **kw: captured.update(id=sid, **kw)
                        or {"id": sid, "lifted": True})
    api_main.app.dependency_overrides[deps.require_admin] = (
        lambda: {"is_admin": True, "email": "op@example.com"}
    )
    res = client.post("/broker-review/suppressions/5/lift",
                      json={"reason": "same person after all"})
    assert res.status_code == 200 and res.json()["lifted"] is True
    assert captured == {"id": 5, "lifted_by": "op@example.com",
                        "reason": "same person after all"}

    # the body is optional — the affordance is one click
    assert client.post("/broker-review/suppressions/5/lift").status_code == 200
    assert captured["reason"] is None


def test_lifting_an_unknown_suppression_is_404_and_a_relift_is_409(client, monkeypatch):
    monkeypatch.setattr(routes.review, "lift_suppression", lambda conn, sid, **kw: None)
    assert client.post("/broker-review/suppressions/9/lift").status_code == 404

    def boom(conn, sid, **kw):
        raise routes.review.MergeError("suppression already lifted")

    monkeypatch.setattr(routes.review, "lift_suppression", boom)
    res = client.post("/broker-review/suppressions/9/lift")
    assert res.status_code == 409 and "already lifted" in res.json()["detail"]


def test_the_suppression_routes_are_gated_like_every_other_broker_review_route():
    """The ledger names identities, brokers and the operator who decided; lifting a
    row re-opens an auto-merge. Asserted through the real dependency (only the DB
    handle is overridden) — a route defined without `require_admin` answers 200."""
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    unauthenticated = TestClient(api_main.app)
    try:
        assert unauthenticated.get("/broker-review/suppressions").status_code == 401
        assert unauthenticated.post(
            "/broker-review/suppressions/5/lift").status_code == 401
    finally:
        api_main.app.dependency_overrides.clear()


def test_operator_merge_source_satisfies_the_events_check_constraint():
    """A fake conn enforces no CHECK, so pin the literal against the migration that
    declares it. merge_brokers wrote source='manual' while migration 186 allows only
    ('auto','operator') — every operator merge through this queue was a 500, and
    prod holds zero 'manual' rows to this day."""
    import pathlib
    import re

    from api import broker_review as review

    module = pathlib.Path(review.__file__).read_text()
    written = set(re.findall(r"%\(reason\)s, '(\w+)' \"", module))
    migration = (pathlib.Path(__file__).resolve().parents[2]
                 / "migrations/186_broker_listing_links_and_queues.sql").read_text()
    allowed = set(re.search(r"check \(source in \(([^)]*)\)\)", migration)[1]
                  .replace("'", "").replace(" ", "").split(","))
    assert written and written <= allowed, (written, allowed)
