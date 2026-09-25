"""Merge safety (migration 559): one price-step definition, no status event on a merge, and
every operator merge / undo recorded as a ruling.

Hermetic: the SQL text and a scripted connection. The executed half — a step never spans
two adverts, a merge writes no status row onto the absorbed property, the rulings land in
`autodedup.verdicts` — runs against the replayed schema in tests/test_merge_safety_live.py.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import pytest

import api.property_merge as pm
from autodedup import ui_sql as usql

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "559_merge_safety.sql"
OP = "operator@example.com"


# --- 1. one price-step definition ------------------------------------------------------


def test_every_price_step_reader_reads_the_one_view_and_none_keeps_a_window():
    from api import notifications as nf
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    readers = {
        "rollup": _RECOMPUTE_BATCH_SQL,
        "watchdog": inspect.getsource(nf._recent_price_drops),
        "collection monitor": inspect.getsource(nf.match_monitored_collections_once),
    }
    for name, source in readers.items():
        assert "listing_price_steps" in source, f"the {name} must read the one step view"
        assert "lag(" not in source, f"the {name} keeps its own price-step window"


def test_the_step_view_compares_an_advert_only_with_its_own_previous_price():
    sql = " ".join(MIGRATION.read_text().split())
    view = sql.split("create view listing_price_steps", 1)[1].split(";", 1)[0]
    assert "p.listing_id = s.listing_id" in view
    assert "property_id =" not in view, "a step keyed on the property spans adverts"
    assert "(p.scraped_at, p.id) < (s.scraped_at, s.id)" in view
    assert "order by p.scraped_at desc, p.id desc limit 1" in view
    assert "s.price_czk <> prev.price_czk" in view
    # No window: a view with one cannot take the callers' join scope (batch, monitored).
    assert " over " not in view.lower()
    assert "revoke all on listing_price_steps from anon, authenticated" in sql
    assert "security_invoker = true" in sql


# --- 2. no status event on a merge -----------------------------------------------------


def test_the_status_trigger_skips_a_row_that_is_or_was_merged_away():
    sql = " ".join(MIGRATION.read_text().split())
    body = sql.split("create or replace function log_property_status_event()", 1)[1]
    assert re.search(
        r"elsif OLD\.is_active is distinct from NEW\.is_active "
        r"and OLD\.status <> 'merged_away' and NEW\.status <> 'merged_away' then",
        body,
    )


def test_the_merge_retires_and_the_unmerge_reactivates_in_one_statement_each():
    """Both halves must touch `status` and `is_active` together, or the trigger sees a
    plain is_active flip on an active row and logs it."""
    from toolkit import property_identity as pi

    merge = " ".join(inspect.getsource(pi.merge_properties).split())
    assert "SET status = 'merged_away', merged_into = %s, merged_at = now(), is_active = false" in merge
    unmerge = " ".join(inspect.getsource(pi.unmerge_group).split())
    assert "SET status = 'active', merged_into = NULL, merged_at = NULL, is_active = EXISTS (" in unmerge


# --- 3. rulings both ways ---------------------------------------------------------------


class _Tx:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Tx":
        self._conn.log.append(("tx", "begin"))
        return self

    def __exit__(self, exc_type: Any, *exc: Any) -> bool:
        self._conn.log.append(("tx", "rollback" if exc_type else "commit"))
        return False


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.log.append((s, params))
        if "FROM properties WHERE id = ANY" in s and "status = 'active'" in s:
            self._rows = [(pid,) for pid in sorted(self._conn.children)]
        elif "SELECT property_id, id FROM listings" in s:
            self._rows = [(pid, lid) for pid in params["ids"]
                          for lid in self._conn.children.get(pid, [])]
        else:
            self._rows = []

    def executemany(self, sql: str, seq: Any) -> None:
        s = " ".join(sql.split())
        for params in seq:
            self._conn.log.append((s, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Conn:
    def __init__(self, children: dict[int, list[int]]) -> None:
        self.children = {k: list(v) for k, v in children.items()}
        self.log: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def writes(self, needle: str) -> list[Any]:
        return [params for sql, params in self.log if needle in sql]


VERDICT = "INSERT INTO autodedup.verdicts"
MNL_UPSERT = "INSERT INTO autodedup.must_not_link"
MNL_RETRACT = "DELETE FROM autodedup.must_not_link"


def _stub_merge(monkeypatch, conn: _Conn) -> None:
    def fake_merge(c, *, survivor_id, retired_id, merge_group_id=None, **_kw):
        conn.log.append(("merge", (survivor_id, retired_id)))
        conn.children[survivor_id] += conn.children.pop(retired_id)
        return {"data": {"merge_group_id": merge_group_id or "g-1", "listings_moved": 1}}

    monkeypatch.setattr(pm, "merge_properties", fake_merge)


def test_a_merge_rules_every_cross_pair_same_and_nothing_inside_a_property(monkeypatch):
    conn = _Conn({3: [30, 31], 7: [70], 9: [90]})
    _stub_merge(monkeypatch, conn)

    result = pm.merge_property_set(conn, [3, 7, 9], decided_by=OP)

    rows = conn.writes(VERDICT)
    pairs = [(r["listing_lo"], r["listing_hi"]) for r in rows]
    assert pairs == [(30, 70), (30, 90), (31, 70), (31, 90), (70, 90)]
    assert (30, 31) not in pairs, "a pair already on one property is no ruling"
    assert {r["verdict"] for r in rows} == {"same"}
    assert {r["decided_by"] for r in rows} == {OP}
    assert {r["note"] for r in rows} == {"operator merge g-1"}
    assert result is not None and result["pairs_ruled_same"] == 5
    # "same" takes back the operator's own veto on each pair; it never writes one.
    assert [(r["listing_lo"], r["listing_hi"]) for r in conn.writes(MNL_RETRACT)] == pairs
    assert conn.writes(MNL_UPSERT) == []


def test_the_merge_rulings_ride_the_merges_own_transaction(monkeypatch):
    conn = _Conn({3: [30], 7: [70]})
    _stub_merge(monkeypatch, conn)
    pm.merge_property_set(conn, [3, 7], decided_by=OP)
    begin, commit = conn.log.index(("tx", "begin")), len(conn.log) - 1
    sides = next(i for i, (s, _) in enumerate(conn.log) if "SELECT property_id, id" in s)
    merge = next(i for i, (s, _) in enumerate(conn.log) if s == "merge")
    verdict = next(i for i, (s, _) in enumerate(conn.log) if VERDICT in s)
    assert begin < sides < merge < verdict < commit
    assert conn.log[commit] == ("tx", "commit")


def _stub_unmerge(monkeypatch, conn: _Conn, *, moved_back: dict[int, list[int]]) -> None:
    def fake_unmerge(c, *, merge_group_id, undone_by):
        assert undone_by == "operator"
        for retired, lids in moved_back.items():
            conn.children[10] = [x for x in conn.children[10] if x not in lids]
            conn.children[retired] = list(lids)
        return {"data": {"merge_group_id": merge_group_id, "survivor_id": 10,
                         "retired_ids": sorted(moved_back), "conflicts": []}}

    monkeypatch.setattr(pm, "unmerge_group", fake_unmerge)


def test_an_unmerge_rules_every_cross_pair_different_with_the_reason(monkeypatch):
    conn = _Conn({10: [100, 101, 200, 300]})
    _stub_unmerge(monkeypatch, conn, moved_back={20: [200], 30: [300]})

    result = pm.unmerge(conn, "grp", decided_by=OP, reason="jiné patro")

    rows = conn.writes(VERDICT)
    pairs = [(r["listing_lo"], r["listing_hi"]) for r in rows]
    assert pairs == [(100, 200), (100, 300), (101, 200), (101, 300), (200, 300)]
    assert {r["verdict"] for r in rows} == {"different"}
    assert "different" in usql.NEGATIVE_VERDICTS, "the adapter's negatives read this value"
    assert all(lo < hi for lo, hi in pairs)
    assert {r["note"] for r in rows} == {"operator unmerge grp: jiné patro"}
    assert [(r["listing_lo"], r["listing_hi"]) for r in conn.writes(MNL_UPSERT)] == pairs
    assert conn.writes(MNL_RETRACT) == []
    assert result["data"]["pairs_ruled_different"] == 5
    assert conn.log[0] == ("tx", "begin") and conn.log[-1] == ("tx", "commit")


def test_an_unmerge_that_moved_nothing_back_rules_nothing(monkeypatch):
    conn = _Conn({10: [100, 200]})
    _stub_unmerge(monkeypatch, conn, moved_back={})
    result = pm.unmerge(conn, "grp", decided_by=OP)
    assert conn.writes(VERDICT) == []
    assert result["data"]["pairs_ruled_different"] == 0


def test_the_ruling_writes_are_the_review_pages_own_statements():
    source = inspect.getsource(pm.record_rulings)
    for name in ("VERDICT_PAIR_UPSERT_SQL", "MUST_NOT_LINK_UPSERT_SQL", "MUST_NOT_LINK_RETRACT_SQL"):
        assert f"usql.{name}" in source
    assert "'pair'" in usql.VERDICT_PAIR_UPSERT_SQL


fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api import dependencies as deps
    from api import main as api_main

    conn = _Conn({10: [100, 200]})
    _stub_unmerge(monkeypatch, conn, moved_back={20: [200]})
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True, "email": OP}
    yield TestClient(api_main.app), conn
    api_main.app.dependency_overrides.clear()


def test_the_undo_route_takes_an_optional_reason_and_rules_as_the_admin(client):
    http, conn = client
    assert http.post("/properties/merges/grp/unmerge").status_code == 200
    assert {r["note"] for r in conn.writes(VERDICT)} == {"operator unmerge grp"}
    assert {r["decided_by"] for r in conn.writes(VERDICT)} == {OP}

    res = http.post("/properties/merges/grp/unmerge", json={"reason": "x" * 501})
    assert res.status_code == 422, "the free-text reason is capped at 500 characters"


def test_a_ruling_needs_an_identity(client):
    from api import dependencies as deps
    from api import main as api_main

    http, _conn = client
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    assert http.post("/properties/merges/grp/unmerge").status_code == 403
    assert http.post("/properties/merge", json={"property_ids": [1, 2]}).status_code == 403
