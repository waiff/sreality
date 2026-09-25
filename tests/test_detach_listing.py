"""The one undo: `toolkit.property_identity.detach_listing` and the routes over it (decision 8).

`_Ledger` is a stateful fake of `listings.property_id`, `properties` and the merge ledger that
serves the statements the set merge and the detach run, so a merge and its undo replay end to
end here (and in tests/test_property_merge_set.py); executed: tests/test_merge_safety_live.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import toolkit.property_identity as pi
from toolkit.property_identity import MergeError, detach_listing, merge_property_set

OP = "operator@example.com"
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Tx:
    def __init__(self, db: "_Ledger") -> None:
        self.db = db

    def __enter__(self) -> "_Tx":
        self.saved = (dict(self.db.listings), dict(self.db.props), [dict(e) for e in self.db.events])
        return self

    def __exit__(self, exc_type: Any, *exc: Any) -> bool:
        if exc_type is not None:
            self.db.listings, self.db.props, self.db.events = self.saved
            self.db.log.append(("rollback", None))
        return False


class _Cur:
    def __init__(self, db: "_Ledger") -> None:
        self.db, self.rows, self.rowcount = db, [], 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self.db.log.append((s, params))
        self.db.count = None
        self.rows = self.db.dispatch(s, params)
        self.rowcount = len(self.rows) if self.db.count is None else self.db.count

    def executemany(self, sql: str, seq: Any) -> None:
        for params in seq:
            self.execute(sql, params)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return list(self.rows)


class _Ledger:
    """listings: id -> property_id; props: id -> status; first_seen / assets / canonical: per
    property; events: the merge ledger."""

    def __init__(self, listings: dict[int, int], *, first_seen: dict[int, datetime] | None = None,
                 assets: dict[int, int] | None = None, canonical: dict[int, int] | None = None,
                 props: dict[int, str] | None = None) -> None:
        self.listings = dict(listings)
        self.props = {pid: "active" for pid in set(listings.values())} | (props or {})
        self.first_seen, self.assets = first_seen or {}, assets or {}
        self.canonical = canonical or {}
        self.events: list[dict[str, Any]] = []
        self.log: list[tuple[str, Any]] = []
        self.count: int | None = None

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def sql(self, needle: str) -> list[Any]:
        return [p for s, p in self.log if needle in s]

    def dispatch(self, s: str, p: Any) -> list[tuple]:  # noqa: C901
        if s.startswith("SELECT id, status, first_seen_at, asset_id FROM properties"):
            return [(pid, self.props[pid], self.first_seen.get(pid, T0), self.assets.get(pid))
                    for pid in sorted(p["ids"]) if pid in self.props]
        if s.startswith("SELECT id, status, category_type, category_main, asset_id"):
            return [(pid, self.props[pid], "prodej", "byt", self.assets.get(pid)) for pid in p]
        if s.startswith("SELECT p.repr_listing_ref_id FROM properties p"):
            return [(self.canonical[pid],) for pid in p["ids"] if pid in self.canonical]
        if s.startswith("INSERT INTO property_merge_events"):
            moved = sorted(lid for lid, pid in self.listings.items() if pid == p["retired"])
            self.events += [{"id": len(self.events) + i + 1, "group": p["group"],
                             "survivor": p["survivor"], "listing": lid, "prev": p["retired"],
                             "undone_by": None} for i, lid in enumerate(moved)]
            self.count = len(moved)
        elif s == "UPDATE listings SET property_id = %s WHERE property_id = %s":
            self.listings = {lid: p[0] if pid == p[1] else pid for lid, pid in self.listings.items()}
        elif s.startswith("UPDATE properties SET status = 'merged_away'"):
            self.props[p[1]] = "merged_away"
        elif s == "SELECT property_id FROM listings WHERE id = %s":
            return [(self.listings[p[0]],)] if p[0] in self.listings else []
        elif s.startswith("SELECT e.listing_ref_id, e.id, e.merge_group_id::text"):
            return [(e["listing"], e["id"], e["group"], e["survivor"], e["prev"])
                    for e in sorted(self.events, key=lambda e: (e["listing"], e["id"]))
                    if e["listing"] in p["ids"] and e["undone_by"] is None]
        elif s.startswith("UPDATE listings SET property_id = %s WHERE id = %s AND"):
            self.count = int(self.listings.get(p[1]) == p[2])
            if self.count:
                self.listings[p[1]] = p[0]
        elif s.startswith("UPDATE property_merge_events SET undone_at = now()"):
            for e in self.events:
                if e["id"] in p[1] and e["undone_by"] is None:
                    e["undone_by"] = p[0]
        elif s.startswith("UPDATE properties p SET status = 'active'"):
            self.count = int(self.props.get(p["pid"]) == "merged_away")
            if self.count:
                self.props[p["pid"]] = "active"
        elif s == "SELECT id FROM listings WHERE property_id = %s":
            return [(lid,) for lid, pid in sorted(self.listings.items()) if pid == p[0]]
        return []


def _merged(db: _Ledger, ids: list[int], *, source: str = "operator") -> dict[str, Any]:
    return merge_property_set(db, ids, source=source, reason="manual_subset",
                              decided_by=OP)["data"]


def _verdicts(db: _Ledger) -> list[tuple[int, int, str, str]]:
    return [(r["listing_lo"], r["listing_hi"], r["verdict"], r["note"])
            for r in db.sql("INSERT INTO autodedup.verdicts")]


def test_one_advert_goes_back_to_the_merged_away_property_it_came_from():
    db = _Ledger({1: 10, 2: 20, 3: 20})
    group = _merged(db, [10, 20])["merge_group_id"]
    out = detach_listing(db, 2, decided_by=OP, reason="jiné patro")["data"]
    assert out == {**out, "detached": True, "outcome": "detached", "listing_id": 2,
                   "survivor_property_id": 10, "restored_property_id": 20,
                   "reactivated": True, "merge_group_ids": [group]}
    assert db.listings == {1: 10, 2: 20, 3: 10} and db.props[20] == "active"
    # the ledger keeps every row; the restored property's card comes from THAT merge's snapshot
    assert [(e["listing"], e["undone_by"]) for e in db.events] == [(2, OP), (3, None)]
    assert db.sql("INSERT INTO property_pipeline (account_id") == [{"g": group, "r": 20, "s": 10}]
    # its sibling joins it without reactivating (or restoring) anything a second time
    assert not detach_listing(db, 3, decided_by=OP)["data"]["reactivated"]
    assert db.listings == {1: 10, 2: 20, 3: 20}
    assert len(db.sql("INSERT INTO property_pipeline (account_id")) == 1


def test_a_second_detach_and_a_native_advert_are_no_ops_that_say_so():
    db = _Ledger({1: 10, 2: 20})
    _merged(db, [10, 20])
    detach_listing(db, 2, decided_by=OP)
    rulings = len(_verdicts(db))
    for lid, where in ((2, 20), (1, 10)):
        again = detach_listing(db, lid, decided_by=OP)["data"]
        assert (again["detached"], again["outcome"], again["rulings_written"]) == (
            False, "not_merged", 0)
        assert again["restored_property_id"] == again["survivor_property_id"] == where
    assert len(_verdicts(db)) == rulings


def test_an_advert_goes_back_to_its_origin_across_a_chain_of_merges():
    """2 came from 20 into 10, then 10 merged into the older 5: the advert returns to the
    record it sat on before every merge that still stands."""
    db = _Ledger({1: 10, 2: 20, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    _merged(db, [10, 20])
    _merged(db, [5, 10])
    assert pi.listing_origins(db, [1, 2, 9]) == {1: 10, 2: 20}
    out = detach_listing(db, 2, decided_by=OP)["data"]
    assert out["restored_property_id"] == 20 and len(out["merge_group_ids"]) == 2
    assert db.listings == {1: 5, 2: 20, 9: 5}
    assert [e["undone_by"] for e in db.events if e["listing"] == 2] == [OP, OP]
    # 5 never held the card of the merge that retired 20: restored, never cleaned off 5
    assert [r["s"] for r in db.sql("INSERT INTO property_pipeline (account_id")] == [None]
    assert db.sql("DELETE FROM property_pipeline WHERE property_id = %(s)s") == []


def test_a_group_scoped_detach_undoes_only_that_merge_while_it_is_the_newest():
    db = _Ledger({1: 10, 2: 20, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    first = _merged(db, [10, 20], source="autodedup")["merge_group_id"]
    second = _merged(db, [5, 10], source="autodedup")["merge_group_id"]
    scoped = {"decided_by": "autodedup-unapply:r", "source": "autodedup"}
    moved_on = detach_listing(db, 2, merge_group_id=first, **scoped)["data"]
    assert moved_on["outcome"] == "moved_on" and db.listings[2] == 5
    back = detach_listing(db, 2, merge_group_id=second, **scoped)["data"]
    assert back["restored_property_id"] == 10 and back["merge_group_ids"] == [second]
    assert db.props[10] == "active" and db.listings[2] == 10
    assert back["rulings_written"] == 0 and db.sql("autodedup.") == [], "an engine undo rules nothing"


def test_an_advert_moved_outside_the_ledger_stays_and_a_missing_one_is_an_error():
    db = _Ledger({1: 10, 2: 20, 7: 70})
    _merged(db, [10, 20])
    db.listings[2] = 70
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.listings[2] == 70
    # moved by a concurrent merge between the ledger read and the property locks: left there
    db.listings[2], dispatch = 10, db.dispatch
    db.dispatch = lambda s, p: (db.listings.update({2: 70}) if "FROM properties WHERE id IN" in s
                                else None) or dispatch(s, p)
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.listings[2] == 70 and db.sql("UPDATE property_merge_events SET undone_at") == []
    with pytest.raises(MergeError):
        detach_listing(db, 99, decided_by=OP)


def test_an_operator_detach_rules_the_advert_different_from_every_advert_that_stays():
    db = _Ledger({1: 10, 2: 10, 3: 20})
    _merged(db, [10, 20])
    db.log.clear()
    out = detach_listing(db, 3, decided_by=OP, reason="jiné patro")["data"]
    note = "operator detach from 10: jiné patro"
    assert _verdicts(db) == [(1, 3, "different", note), (2, 3, "different", note)]
    assert out["rulings_written"] == 2
    assert {(r["listing_lo"], r["listing_hi"]) for r in
            db.sql("INSERT INTO autodedup.must_not_link")} == {(1, 3), (2, 3)}
    assert {r["decided_by"] for r in db.sql("INSERT INTO autodedup.verdicts")} == {OP}


def test_a_detach_leaves_curation_where_it_is_and_recomputes_both_once():
    db = _Ledger({1: 10, 2: 20})
    _merged(db, [10, 20])
    db.log.clear()
    detach_listing(db, 2, decided_by=OP)
    written = " ".join(s for s, _p in db.log)
    for table in ("collection_properties", "property_tags", "property_notes",
                  "notification_dispatches", "property_dismissals", "property_status_events",
                  "DELETE FROM properties", "DELETE FROM property_merge_events"):
        assert table not in written, f"a detach touched {table}"
    assert db.sql("recompute_property_mf") == [(10,), (20,)]
    assert db.sql("DELETE FROM browse_list") == [([10, 20],)]
    (reactivate,) = [s for s, _p in db.log if "SET status = 'active'" in s]
    assert "merged_into = NULL, merged_at = NULL, is_active = EXISTS (" in reactivate


def test_merging_then_detaching_every_advert_restores_every_original_property():
    """W3's gate at unit scale — a refactor of the mechanics changes no grouping: three set
    merges, one built on another; every advert detached in turn returns to its own record."""
    original = {1: 10, 2: 10, 3: 20, 4: 30, 5: 30, 6: 40, 7: 50, 8: 60, 9: 70, 11: 80,
                12: 80, 13: 20}
    db = _Ledger(original, first_seen={pid: T0 + timedelta(days=pid)
                                       for pid in set(original.values())})
    _merged(db, [10, 20, 30])
    _merged(db, [40, 50], source="autodedup")
    _merged(db, [60, 10, 80])
    assert len(set(db.listings.values())) == 3
    for lid in sorted(original):
        detach_listing(db, lid, decided_by=OP)
    assert db.listings == original and set(db.props.values()) == {"active"}
    assert all(e["undone_by"] for e in db.events), "a detach left a live ledger row behind"


# --- the routes --------------------------------------------------------------------------


fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import api.property_merge as pm
    from api import dependencies as deps
    from api import main as api_main

    db = _Ledger({1: 10, 2: 20, 3: 30})
    _merged(db, [10, 20])
    db.log.clear()
    monkeypatch.setattr(pm, "resolve_active_property_id",
                        lambda conn, pid: {10: 10, 20: 10}.get(pid))
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: db
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True, "email": OP}
    yield TestClient(api_main.app), db
    api_main.app.dependency_overrides.clear()


def test_the_detach_route_returns_the_contract_and_a_second_click_moves_nothing(client):
    http, db = client
    res = http.post("/properties/10/detach", json={"listing_id": 2, "reason": "jiné patro"})
    assert res.status_code == 200
    assert res.json() == {"listing_id": 2, "detached": True, "outcome": "detached",
                          "survivor_property_id": 10, "restored_property_id": 20,
                          "rulings_written": 1}
    assert _verdicts(db) == [(1, 2, "different", "operator detach from 10: jiné patro")]
    # a stale property id follows merged_into; the advert is no longer on it
    again = http.post("/properties/20/detach", json={"listing_id": 2})
    assert again.status_code == 200 and again.json()["outcome"] == "not_on_property"
    assert again.json()["survivor_property_id"] == 10 and db.listings[2] == 20


def test_the_detach_route_validates_its_input_and_needs_an_identity(client):
    from api import dependencies as deps
    from api import main as api_main

    http, db = client
    assert http.post("/properties/10/detach",
                     json={"listing_id": 2, "reason": "x" * 501}).status_code == 422
    assert http.post("/properties/10/detach", json={"listing_id": 99}).status_code == 404
    assert http.post("/properties/404/detach", json={"listing_id": 2}).status_code == 404
    assert http.post("/properties/merges/grp/unmerge").status_code in (404, 405)
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    assert http.post("/properties/10/detach", json={"listing_id": 2}).status_code == 403
    assert http.post("/properties/merge", json={"property_ids": [1, 2]}).status_code == 403
    assert db.listings[2] == 10


def test_the_origins_route_says_where_each_advert_came_from(client):
    http, _db = client
    res = http.get("/properties/10/origins")
    assert res.status_code == 200
    assert res.json() == {"property_id": 10, "adverts": [
        {"listing_id": 1, "origin_property_id": None},
        {"listing_id": 2, "origin_property_id": 20}]}
