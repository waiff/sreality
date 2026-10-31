"""`POST /properties/merge` (api/property_merge.py): the one merge plus the receipt of the acting
account (MS15, MS13), and rule 15's refusal in Czech naming the two properties; `GET
/properties/merge`, the count of "different" rulings a merge would take back (MS12); the split
routes' acting account. Over tests/_property_ledger's fake; the receipt's SQL runs in
tests/test_property_carriers_live.py, the split's in tests/test_property_split_live.py."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

pytest.importorskip("fastapi")

import api.property_merge as pm  # noqa: E402
from tests._property_ledger import OP, _Ledger, ledger_carriers  # noqa: E402,F401 — the fixture

pytestmark = pytest.mark.usefixtures("ledger_carriers")

ADMIN = {"is_admin": True, "email": OP}


@pytest.fixture()
def merge():
    from fastapi.testclient import TestClient

    from api import dependencies as deps
    from api import main as api_main

    def post(db: _Ledger, letters: dict[int, str], claims: dict[str, Any]) -> Any:
        api_main.app.dependency_overrides[deps.get_db_conn] = lambda: db
        api_main.app.dependency_overrides[deps.require_admin] = lambda: claims
        return TestClient(api_main.app).post("/properties/merge", json={"letters": letters})

    yield post
    api_main.app.dependency_overrides.clear()


def test_the_receipt_is_the_acting_accounts_and_empty_without_one(merge, monkeypatch):
    account = uuid.uuid4()
    monkeypatch.setattr(pm.tenant_pool, "resolve_account_id",
                        lambda conn, claims: account if claims["sub"] == "user-1" else None)
    asked: list[Any] = []
    real = pm.merge_receipt
    monkeypatch.setattr(pm, "merge_receipt", lambda conn, merged, acc: asked.append(
        (merged["survivor_id"], acc)) or real(conn, merged, None))
    body = merge(_Ledger({1: 10, 2: 20}), {20: "A", 10: "A"}, {**ADMIN, "sub": "user-1"}).json()
    body = body["letters"]["A"]
    assert (body["survivor_id"], body["retired_ids"], body["rulings_taken_back"]) == (10, [20], 0)
    assert asked == [(10, account)]
    for claims in ({**ADMIN, "sub": "user-2"}, ADMIN):
        body = merge(_Ledger({1: 10, 2: 20}), {10: "A", 20: "A"}, claims).json()["letters"]["A"]
        assert (body["carried"], body["hidden_for_you"]) == (
            {"notes": 0, "pipeline": None, "collections": [], "tags": []}, False)
    assert asked[1:] == [(10, None), (10, None)]


def test_no_read_around_the_merge_reports_a_committed_merge_as_failed(merge, monkeypatch):
    """The account is read before anything is written; a receipt read failing after the commit
    answers the merge with the receipt unknown (null), not an error."""
    def gone(*_args: Any) -> Any:
        raise psycopg.OperationalError("connection lost")

    db = _Ledger({1: 10, 2: 20})
    monkeypatch.setattr(pm.tenant_pool, "resolve_account_id", gone)
    with pytest.raises(psycopg.OperationalError):
        merge(db, {10: "A", 20: "A"}, {**ADMIN, "sub": "user-1"})
    assert db.listings == {1: 10, 2: 20}
    monkeypatch.setattr(pm.tenant_pool, "resolve_account_id", lambda conn, claims: uuid.uuid4())
    monkeypatch.setattr(pm, "merge_receipt", gone)
    body = merge(db, {10: "A", 20: "A"}, {**ADMIN, "sub": "user-1"}).json()["letters"]["A"]
    assert (body["survivor_id"], body["carried"], body["hidden_for_you"]) == (10, None, None)
    assert db.listings == {1: 10, 2: 10}


CLASH = ("Inzerát v kategorii Byty a inzerát v kategorii Domy systém nikdy nespojí do jedné "
         "nemovitosti (výjimkou jsou jen dvojice dům – komerční objekt, dům – pozemek, komerční "
         "objekt – pozemek a byt – komerční objekt), proto nemohou mít stejné písmeno. Dejte jim "
         "různá písmena.")


def test_a_category_clash_is_refused_in_czech_naming_both_properties(merge):
    db = _Ledger({1: 10, 2: 20}, ad_cats={1: ("prodej", "byt"), 2: ("prodej", "dum")})
    res = merge(db, {10: "A", 20: "A"}, ADMIN)
    assert res.status_code == 409 and db.listings == {1: 10, 2: 20}
    assert res.json()["detail"] == {"code": "refused", "ids": [10, 20], "message": CLASH}
    gone = merge(_Ledger({1: 10}, props={20: "merged_away"}), {10: "A", 20: "A"}, ADMIN)
    assert gone.json()["detail"].startswith("properties not found")


def test_a_statement_merges_every_letter_in_one_transaction_or_nothing(merge):
    """Two letters are two merges with two receipts; a clash in one letter names that letter and
    leaves the other unmerged; a lone letter is a slip, refused before anything is read."""
    db = _Ledger({1: 10, 2: 20, 3: 30, 4: 40})
    body = merge(db, {10: "A", 20: "A", 30: "B", 40: "B"}, ADMIN).json()["letters"]
    assert {k: (v["survivor_id"], v["retired_ids"]) for k, v in body.items()} == {
        "A": (10, [20]), "B": (30, [40])}
    assert body["A"]["merge_group_id"] != body["B"]["merge_group_id"]
    assert db.listings == {1: 10, 2: 10, 3: 30, 4: 30}
    db = _Ledger({1: 10, 2: 20, 3: 30, 4: 40}, ad_cats={3: ("prodej", "byt"), 4: ("prodej", "dum")})
    res = merge(db, {10: "A", 20: "A", 30: "B", 40: "B"}, ADMIN)
    assert res.status_code == 409 and db.listings == {1: 10, 2: 20, 3: 30, 4: 40}
    assert res.json()["detail"] == {"code": "refused", "ids": [30, 40],
                                    "message": f"Písmeno B: {CLASH}"}
    db = _Ledger({1: 10, 2: 20, 3: 30, 4: 40})
    for slip in ({10: "A", 20: "A", 30: "B"}, {10: "A"}, {10: "A", 20: "a"}):
        assert merge(db, slip, ADMIN).status_code == 400
    assert db.log == [] and db.listings == {1: 10, 2: 20, 3: 30, 4: 40}


def test_the_merge_list_routes_are_gone():
    """MS16/MS21: no screen read them; the toast and the carry record are the record."""
    from tests.api._routes import method_routes

    paths = {path for _method, path, _route in method_routes()}
    assert "/properties/merge" in paths
    assert not paths & {"/properties/merges", "/properties/merged"}


@pytest.fixture()
def api():
    from fastapi.testclient import TestClient

    from api import dependencies as deps
    from api import main as api_main

    def client(db: _Ledger, claims: dict[str, Any]) -> Any:
        api_main.app.dependency_overrides[deps.get_db_conn] = lambda: db
        api_main.app.dependency_overrides[deps.require_admin] = lambda: claims
        return TestClient(api_main.app)

    yield client
    api_main.app.dependency_overrides.clear()


def test_the_merge_preview_counts_what_the_merge_would_take_back(api):
    db = _Ledger({1: 10, 2: 20, 3: 20}, canonical={10: 1, 20: 2})
    db.rule(1, 3, "different")
    db.rule(2, 3, "different")                  # inside 20: not between what the merge joins
    http = api(db, ADMIN)
    res = http.get("/properties/merge", params={"letters": "20:A,10:A"})
    assert res.status_code == 200
    assert res.json() == {"letters": {"A": {"property_ids": [10, 20], "rulings_taken_back": 1}}}
    assert not [s for s, _p in db.log if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    for bad in ("10:A", "10:A,x:A", "10:A,10:A", "10:A,20:a", "10:A,20:B"):
        assert http.get("/properties/merge", params={"letters": bad}).status_code == 400
    merged = http.post("/properties/merge", json={"letters": {"10": "A", "20": "A"}}).json()
    assert merged["letters"]["A"]["rulings_taken_back"] == 1


def test_the_split_routes_show_and_move_the_acting_accounts_items_only(api, monkeypatch):
    """The account is read from the admin's own claims (none without `sub`) and handed to the
    toolkit, which shows and routes that account's items alone."""
    from toolkit import property_carriers as carriers

    account = uuid.uuid4()
    monkeypatch.setattr(pm.tenant_pool, "resolve_account_id",
                        lambda conn, claims: account if claims["sub"] == "user-1" else None)
    seen: list[Any] = []
    monkeypatch.setattr(carriers, "curation_plan",
                        lambda cur, **kw: seen.append(kw.get("account")) or [])
    db = _Ledger({1: 10, 2: 10})
    for claims, who in ((ADMIN, None), ({**ADMIN, "sub": "user-2"}, None),
                        ({**ADMIN, "sub": "user-1"}, account)):
        http = api(db, claims)
        plan = http.get("/properties/10/split", params={"letters": "1:A,2:B"}).json()["plan"]
        assert seen[-1] == who
        if who is None:
            continue
        res = http.post("/properties/10/split", json={
            "letters": {"1": "A", "2": "B"}, "expect": plan})
        assert res.status_code == 200 and seen[-1] == account
