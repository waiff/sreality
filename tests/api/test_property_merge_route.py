"""`POST /properties/merge` (api/property_merge.py): the one merge plus the receipt of the acting
account (MS15, MS13), and rule 15's refusal in Czech naming the two properties. Over
tests/_property_ledger's fake; the receipt's SQL runs in tests/test_property_carriers_live.py."""

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

    def post(db: _Ledger, ids: list[int], claims: dict[str, Any]) -> Any:
        api_main.app.dependency_overrides[deps.get_db_conn] = lambda: db
        api_main.app.dependency_overrides[deps.require_admin] = lambda: claims
        return TestClient(api_main.app).post("/properties/merge", json={"property_ids": ids})

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
    body = merge(_Ledger({1: 10, 2: 20}), [20, 10], {**ADMIN, "sub": "user-1"}).json()
    assert (body["survivor_id"], body["retired_ids"], body["rulings_taken_back"]) == (10, [20], 0)
    assert asked == [(10, account)]
    for claims in ({**ADMIN, "sub": "user-2"}, ADMIN):
        body = merge(_Ledger({1: 10, 2: 20}), [10, 20], claims).json()
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
        merge(db, [10, 20], {**ADMIN, "sub": "user-1"})
    assert db.listings == {1: 10, 2: 20}
    monkeypatch.setattr(pm.tenant_pool, "resolve_account_id", lambda conn, claims: uuid.uuid4())
    monkeypatch.setattr(pm, "merge_receipt", gone)
    body = merge(db, [10, 20], {**ADMIN, "sub": "user-1"}).json()
    assert (body["survivor_id"], body["carried"], body["hidden_for_you"]) == (10, None, None)
    assert db.listings == {1: 10, 2: 10}


def test_a_category_clash_is_refused_in_czech_naming_both_properties(merge):
    db = _Ledger({1: 10, 2: 20}, ad_cats={1: ("prodej", "byt"), 2: ("prodej", "dum")})
    res = merge(db, [10, 20], ADMIN)
    assert res.status_code == 409 and db.listings == {1: 10, 2: 20}
    assert res.json()["detail"] == {"code": "refused", "ids": [10, 20], "message": (
        "Inzerát v kategorii Byty a inzerát v kategorii Domy systém nikdy nespojí do jedné "
        "nemovitosti (výjimkou jsou jen dvojice dům – komerční objekt, dům – pozemek, komerční "
        "objekt – pozemek a byt – komerční objekt), proto je nelze sloučit.")}
    gone = merge(_Ledger({1: 10}, props={20: "merged_away"}), [10, 20], ADMIN)
    assert gone.json()["detail"].startswith("properties not found")


def test_the_merge_list_routes_are_gone():
    """MS16/MS21: no screen read them; the toast and the carry record are the record."""
    from tests.api.test_admin_route_coverage import _api_routes

    paths = {path for _method, path, _route in _api_routes()}
    assert "/properties/merge" in paths
    assert not paths & {"/properties/merges", "/properties/merged"}
