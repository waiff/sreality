"""Hermetic tests for the sold-comps cell fetch (no network, no DB).

A fake client hands back canned `__NEXT_DATA__` pages and a fake connection records
every statement, so what is pinned here is the CONTRACT: how far a walk goes, that a
cell attempt always ends in the ledger, and that a page the parser refuses stores
nothing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scraper import sold_db, sold_fetch
from scraper.reas_parser import ReasPayloadError

_BOX = sold_db.CellBox(
    sw_lat=49.5, sw_lng=17.1, ne_lat=49.7, ne_lng=17.4,
    ewkt="SRID=4326;POLYGON((17.1 49.5,17.4 49.5,17.4 49.7,17.1 49.7,17.1 49.5))",
)


def _record(**over: Any) -> dict[str, Any]:
    record = {
        "mapPointerId": "12345_building_678",
        "type": "building",
        "soldPrice": 5_400_000,
        "soldAt": "2026-08-21T00:00:00",
        "price": 5_700_000,
        "point": {"coordinates": [17.25, 49.6]},
        "utilityArea": 120.0,
        "landArea": 640.0,
        "formattedAddress": "Dlouhá 1, Olomouc",
        "municipalityId": 500496,
        "link": "https://www.reas.cz/prodane/inzerat-x",
        "imagesWithMetadata": [{"original": "https://img/1.jpg"}],
    }
    record.update(over)
    return record


def _page(records: list[dict[str, Any]], *, next_page: int | None = None) -> str:
    payload = {"props": {"pageProps": {
        "adsListParams": {"linkedToTransfer": True},
        "adsListResult": {
            "data": records, "count": len(records), "possibleCount": 625,
            "nextPage": next_page,
        },
    }}}
    return f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>'


class _FakeClient:
    """Serves the scripted pages in `listPage` order and records the bounds asked."""

    def __init__(self, pages: dict[int, str]) -> None:
        self._pages = pages
        self.asked: list[tuple[tuple[float, ...], int]] = []

    def fetch_sold_page(self, bounds, *, page: int = 1) -> str:
        self.asked.append((bounds, page))
        return self._pages[page]


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[tuple]:
        return [(True,)] * self._conn.inserted

    def fetchone(self) -> tuple | None:
        return None


class _FakeConn:
    def __init__(self, inserted: int = 0) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.inserted = inserted

    def cursor(self) -> _Cur:
        return _Cur(self)


@pytest.fixture
def cell(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`obec_cell_box` answers _BOX; the writes are captured, not executed."""
    captured: dict[str, Any] = {"upserts": [], "ledger": []}
    monkeypatch.setattr(sold_db, "obec_cell_box", lambda conn, kod: _BOX)
    monkeypatch.setattr(
        sold_db, "upsert_sold_transactions",
        lambda conn, rows: captured["upserts"].append(list(rows)) or len(rows),
    )
    monkeypatch.setattr(
        sold_db, "record_fetch",
        lambda conn, **kw: captured["ledger"].append(kw),
    )
    return captured


def test_a_single_page_cell_asks_once_and_records_one_ok_row(cell):
    client = _FakeClient({1: _page([_record()])})

    out = sold_fetch.fetch_cell(_FakeConn(), client, 500496)

    assert client.asked == [((49.5, 17.1, 49.7, 17.4), 1)]
    assert out["status"] == "ok"
    assert (out["records"], out["pages"], out["source_total"]) == (1, 1, 625)
    assert cell["ledger"] == [{
        "source": "reas", "obec_kod": 500496, "bbox": _BOX.ewkt, "status": "ok",
        "record_count": 1, "source_total": 625, "pages": 1,
    }]


def test_the_walk_follows_next_page_until_the_source_says_stop(cell):
    client = _FakeClient({
        1: _page([_record(mapPointerId="1_building_1")], next_page=2),
        2: _page([_record(mapPointerId="2_building_2")], next_page=3),
        3: _page([_record(mapPointerId="3_building_3")]),
    })

    out = sold_fetch.fetch_cell(_FakeConn(), client, 500496)

    assert [page for _, page in client.asked] == [1, 2, 3]
    assert (out["records"], out["pages"]) == (3, 3)
    assert cell["ledger"][0]["pages"] == 3


def test_the_page_cap_bounds_a_runaway_cell(cell):
    # Every page claims another one; the cap is what ends the walk, and `pages` in
    # the ledger is the only tell that it bound.
    client = _FakeClient({n: _page([_record(mapPointerId=f"{n}_building_{n}")],
                                   next_page=n + 1) for n in range(1, 10)})

    out = sold_fetch.fetch_cell(_FakeConn(), client, 500496, max_pages=4)

    assert out["pages"] == 4
    assert [page for _, page in client.asked] == [1, 2, 3, 4]
    assert cell["ledger"][0]["status"] == "ok"


def test_a_zero_yield_cell_still_writes_its_ledger_row(cell):
    # "We asked this cell and it held nothing" is a fact — the one that separates an
    # empty answer from a lane that never ran.
    out = sold_fetch.fetch_cell(_FakeConn(), _FakeClient({1: _page([])}), 500496)

    assert (out["status"], out["records"]) == ("ok", 0)
    assert cell["ledger"][0]["record_count"] == 0


def test_a_broker_reported_record_is_dropped_and_counted_not_failed(cell):
    # ~1% of the feed is the source's own self-reported sale (an ObjectId transfer
    # id). A different kind of claim, not a defect: it must not fail the page.
    client = _FakeClient({1: _page([
        _record(),
        _record(mapPointerId="507f1f77bcf86cd799439011_unit_1-2-3"),
    ])})

    out = sold_fetch.fetch_cell(_FakeConn(), client, 500496)

    assert (out["status"], out["records"], out["dropped"]) == ("ok", 1, 1)


def test_a_refused_page_stores_nothing_and_fails_the_whole_cell(cell):
    # W1's open issue, decided: the parser's refusals are contract failures, so the
    # page is dropped whole rather than stored 90% true.
    client = _FakeClient({1: _page([_record(), _record(type="parcel")])})

    out = sold_fetch.fetch_cell(_FakeConn(), client, 500496)

    assert out["status"] == "failed"
    assert "unknown reas type" in out["error"]
    assert cell["upserts"] == [], "a refused page writes no rows at all"
    assert cell["ledger"][0]["status"] == "failed"
    assert cell["ledger"][0]["bbox"] == _BOX.ewkt


def test_a_transport_failure_ends_in_the_ledger_and_never_raises(cell):
    class _Broken:
        def fetch_sold_page(self, bounds, *, page: int = 1) -> str:
            raise TimeoutError("read timed out")

    out = sold_fetch.fetch_cell(_FakeConn(), _Broken(), 500496)

    assert out["status"] == "failed"
    assert out["error"] == "TimeoutError: read timed out"
    assert cell["ledger"][0]["status"] == "failed"


def test_the_ledger_error_is_truncated(cell):
    class _Loud:
        def fetch_sold_page(self, bounds, *, page: int = 1) -> str:
            raise ReasPayloadError("x" * 5000)

    sold_fetch.fetch_cell(_FakeConn(), _Loud(), 500496)

    assert len(cell["ledger"][0]["error"]) == sold_fetch._ERROR_MAX


def test_a_cell_with_no_polygon_is_skipped_without_a_ledger_row(monkeypatch):
    # The ledger is keyed on the box; with no polygon there is no cell to record.
    monkeypatch.setattr(sold_db, "obec_cell_box", lambda conn, kod: None)
    written: list[Any] = []
    monkeypatch.setattr(sold_db, "record_fetch", lambda *a, **k: written.append(k))

    out = sold_fetch.fetch_cell(_FakeConn(), _FakeClient({}), 999999)

    assert out["status"] == "skipped"
    assert written == []


def test_a_failing_ledger_write_never_escapes_the_cell(monkeypatch):
    monkeypatch.setattr(sold_db, "obec_cell_box", lambda conn, kod: _BOX)

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("pooler gone")

    monkeypatch.setattr(sold_db, "upsert_sold_transactions", lambda conn, rows: 0)
    monkeypatch.setattr(sold_db, "record_fetch", boom)

    out = sold_fetch.fetch_cell(
        _FakeConn(), _FakeClient({1: _page([_record()])}), 500496)

    assert out["status"] == "failed"
