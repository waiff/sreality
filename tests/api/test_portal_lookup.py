"""Tests for POST /listings/lookup — the Chrome extension's batch (source,
native id) → MF rent/yield + sreality_id (app deep-link) + latest-estimate.

The logic tests drive `lookup_portal_listings` against a tiny fake cursor that
returns dict rows (the real query uses psycopg's dict_row factory); the route
tests exercise the bearer gate, request validation, and delegation.
"""

from __future__ import annotations

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
# Logic: lookup_portal_listings against a fake dict-row cursor.
# ----------------------------------------------------------------------

def _mk_row(source: str, source_id: str, found: bool, **cols: Any) -> dict[str, Any]:
    """One dict row keyed by the SELECT aliases (defaults null)."""
    row: dict[str, Any] = {
        "source": source, "source_id": source_id, "found": found,
        "sreality_id": None, "category_main": None, "category_type": None,
        "area_m2": None, "price_czk": None, "disposition": None,
        "district": None, "locality": None, "is_active": None,
        "last_seen_at": None, "mf_reference_rent_czk": None,
        "mf_gross_yield_pct": None, "estimation_id": None,
        "estimation_kind": None, "estimation_yield": None,
    }
    row.update(cols)
    return row


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: tuple[str, list[Any]] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: list[Any]) -> None:
        self.executed = (sql, params)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cur = _FakeCursor(rows)

    def cursor(self, **_kwargs: Any) -> _FakeCursor:  # accepts row_factory=dict_row
        return self.cur


_TS = datetime(2026, 6, 1, 12, 0, 0)


def _items(*pairs: tuple[str, str]) -> list[s.PortalLookupItem]:
    return [s.PortalLookupItem(source=src, source_id=sid) for src, sid in pairs]


def test_lookup_maps_rows_with_sreality_id_mf_and_estimation() -> None:
    rows = [
        # sreality: found, has MF + a successful estimation; positive sreality_id
        _mk_row("sreality", "1184977484", True, sreality_id=1184977484,
                category_main="byt", category_type="prodej", area_m2=Decimal("65.0"),
                price_czk=4_800_000, disposition="2+kk", district="Praha 5",
                locality="Praha 5 - Smíchov", is_active=True, last_seen_at=_TS,
                mf_reference_rent_czk=21_840, mf_gross_yield_pct=Decimal("5.46"),
                estimation_id=99, estimation_kind="rent",
                estimation_yield=Decimal("5.46")),
        # bazos: found, NEGATIVE synthetic sreality_id, no MF, no estimation
        _mk_row("bazos", "220291221", True, sreality_id=-187691,
                category_main="byt", category_type="prodej", price_czk=7_700_000,
                disposition="3+kk", district="okres Pardubice", is_active=True),
        # idnes: not found → sreality_id null
        _mk_row("idnes", "deadbeef", False),
    ]
    out = pl.lookup_portal_listings(
        _FakeConn(rows),
        _items(("sreality", "1184977484"), ("bazos", "220291221"),
               ("idnes", "deadbeef")),
    )
    data = out["data"]
    assert [d["source"] for d in data] == ["sreality", "bazos", "idnes"]

    sr = data[0]
    assert sr["found"] is True
    assert sr["sreality_id"] == 1184977484  # positive for sreality
    assert sr["area_m2"] == 65.0  # Decimal → float
    assert sr["mf_gross_yield_pct"] == 5.46
    assert sr["last_seen_at"] == _TS.isoformat()  # datetime → iso8601
    assert sr["latest_estimation"] == {
        "estimation_id": 99, "estimate_kind": "rent", "gross_yield_pct": 5.46,
    }

    bz = data[1]
    assert bz["found"] is True
    assert bz["sreality_id"] == -187691  # negative synthetic for non-sreality
    assert bz["latest_estimation"] is None

    idn = data[2]
    assert idn["found"] is False
    assert idn["sreality_id"] is None  # not in our DB → no app page
    assert idn["latest_estimation"] is None


def test_lookup_binds_one_value_pair_per_item() -> None:
    out_conn = _FakeConn([])
    pl.lookup_portal_listings(out_conn, _items(("sreality", "1"), ("idnes", "abc")))
    sql, params = out_conn.cur.executed
    # two (%s::text, %s::text) tuples → 4 bound params, in request order
    assert params == ["sreality", "1", "idnes", "abc"]
    assert sql.count("%s") == 4


def test_lookup_preserves_request_order_even_if_db_reorders() -> None:
    rows = [
        _mk_row("idnes", "b", True, sreality_id=-2, category_main="byt",
                category_type="prodej", is_active=True),
        _mk_row("sreality", "a", True, sreality_id=1, category_main="byt",
                category_type="prodej", is_active=True),
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
