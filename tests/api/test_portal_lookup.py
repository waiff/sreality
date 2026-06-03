"""Tests for POST /listings/lookup — the Chrome extension's batch (source,
native id) → MF rent/yield + latest-estimate lookup.

The logic tests drive `lookup_portal_listings` against a tiny fake cursor
(one canned result set per query); the route tests exercise the bearer gate,
request validation, and delegation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import portal_lookup as pl
from api import schemas as s


# ----------------------------------------------------------------------
# Logic: lookup_portal_listings against a fake cursor.
# ----------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed: tuple[str, list[Any]] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: list[Any]) -> None:
        self.executed = (sql, params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.cur = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self.cur


_TS = datetime(2026, 6, 1, 12, 0, 0)


def _items(*pairs: tuple[str, str]) -> list[s.PortalLookupItem]:
    return [s.PortalLookupItem(source=src, source_id=sid) for src, sid in pairs]


def test_lookup_maps_rows_with_mf_and_estimation() -> None:
    rows = [
        # sreality: found, has MF + a successful estimation
        ("sreality", "1184977484", True, "byt", "prodej", Decimal("65.0"),
         4_800_000, "2+kk", "Praha 5", "Praha 5 - Smíchov", True, _TS,
         21_840, Decimal("5.46"), 99, "rent", Decimal("5.46")),
        # idnes: found, has MF but no estimation
        ("idnes", "69efb4b677527bbe3f0ee7d5", True, "byt", "prodej",
         Decimal("253.0"), 83_000_000, "5+1", "Praha 1", "Praha 1", True, _TS,
         71_852, Decimal("1.04"), None, None, None),
        # bazos: not found
        ("bazos", "219122924", False, None, None, None, None, None, None, None,
         None, None, None, None, None, None, None),
    ]
    conn = _FakeConn(rows)
    out = pl.lookup_portal_listings(
        conn,
        _items(("sreality", "1184977484"),
               ("idnes", "69efb4b677527bbe3f0ee7d5"),
               ("bazos", "219122924")),
    )
    data = out["data"]
    assert [d["source"] for d in data] == ["sreality", "idnes", "bazos"]

    sr = data[0]
    assert sr["found"] is True
    assert sr["category_main"] == "byt" and sr["category_type"] == "prodej"
    assert sr["area_m2"] == 65.0  # Decimal → float
    assert sr["price_czk"] == 4_800_000
    assert sr["mf_reference_rent_czk"] == 21_840
    assert sr["mf_gross_yield_pct"] == 5.46
    assert sr["last_seen_at"] == _TS.isoformat()  # datetime → iso8601
    assert sr["latest_estimation"] == {
        "estimation_id": 99, "estimate_kind": "rent", "gross_yield_pct": 5.46,
    }

    assert data[1]["found"] is True
    assert data[1]["latest_estimation"] is None

    bz = data[2]
    assert bz["found"] is False
    assert bz["mf_gross_yield_pct"] is None
    assert bz["latest_estimation"] is None


def test_lookup_binds_one_value_pair_per_item() -> None:
    conn = _FakeConn([])
    pl.lookup_portal_listings(conn, _items(("sreality", "1"), ("idnes", "abc")))
    sql, params = conn.cur.executed
    # two (%s::text, %s::text) tuples → 4 bound params, in request order
    assert params == ["sreality", "1", "idnes", "abc"]
    assert sql.count("%s") == 4


def test_lookup_preserves_request_order_even_if_db_reorders() -> None:
    # DB returns rows in a different order than requested; output follows the
    # request order (we key by (source, source_id)).
    rows = [
        ("idnes", "b", True, "byt", "prodej", None, None, None, None, None,
         True, None, None, None, None, None, None),
        ("sreality", "a", True, "byt", "prodej", None, None, None, None, None,
         True, None, None, None, None, None, None),
    ]
    out = pl.lookup_portal_listings(
        _FakeConn(rows), _items(("sreality", "a"), ("idnes", "b")),
    )
    assert [d["source_id"] for d in out["data"]] == ["a", "b"]


# ----------------------------------------------------------------------
# Route: POST /listings/lookup
# ----------------------------------------------------------------------

@pytest.fixture()
def client() -> Any:
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_route_delegates_and_returns_data(client, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_lookup(conn, items):
        captured["items"] = items
        return {"data": [{"source": "sreality", "source_id": "1", "found": True}]}

    monkeypatch.setattr(api_main, "lookup_portal_listings", fake_lookup)
    res = client.post(
        "/listings/lookup",
        json={"items": [{"source": "sreality", "source_id": "1"}]},
    )
    assert res.status_code == 200
    assert res.json()["data"][0]["source"] == "sreality"
    assert [(i.source, i.source_id) for i in captured["items"]] == [("sreality", "1")]


def test_route_rejects_empty_items(client) -> None:
    res = client.post("/listings/lookup", json={"items": []})
    assert res.status_code == 422


def test_route_rejects_over_50_items(client) -> None:
    items = [{"source": "sreality", "source_id": str(i)} for i in range(51)]
    res = client.post("/listings/lookup", json={"items": items})
    assert res.status_code == 422


def test_route_requires_token_when_set(client, monkeypatch) -> None:
    monkeypatch.setenv("API_TOKEN", "secret")
    res = client.post(
        "/listings/lookup",
        json={"items": [{"source": "sreality", "source_id": "1"}]},
    )
    assert res.status_code == 401
